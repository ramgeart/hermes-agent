"""TemporalContextEngine — context engine with temporal memory awareness.

Extends ContextEngine ABC to produce a structured temporal context string
with five blocks: identity, recent 14-day index, current week episode,
semantic memory, and detected patterns.

This engine delegates compression to the default ContextCompressor while
adding the ``build()`` method for temporal context generation.

Cache Strategy
--------------
The engine uses an in-memory, per-user, per-block cache with independent TTLs
to avoid recalculating expensive DB queries every turn:

    IDENTITY         — 3600s (1h)  : changes very rarely (immutable after set)
    ÚLTIMOS 14 DÍAS  —  300s (5m)  : new sessions arrive frequently
    EPISODIO ACTUAL   —  300s (5m)  : changes as sessions within the week land
    MEMORIA SEMÁNTICA —  600s (10m) : fact store updates are less frequent
    PATRONES          —  600s (10m) : patterns shift slowly over many sessions

Cache structure::

    _cache[user_id][block_name] = (content: str | None, timestamp: float)

On ``build()`` or ``build_progressive()``, each block is checked against its
TTL. Stale blocks are regenerated independently and merged with fresh cached
blocks (partial cache hit). Callers never see the cache — ``build()`` always
returns fresh-enough context.

Invalidation:
  - ``invalidate(user_id, block=None)`` clears one or all blocks for a user.
  - External callers (e.g. message store, fact store) should call
    ``invalidate()`` when new data arrives so the next turn gets fresh context.

Thread safety:
  All cache reads/writes are guarded by a ``threading.Lock`` to support
  concurrent calls from multiple agent sessions sharing one engine instance.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agent.context_engine import ContextEngine

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────

_DAYS_ES = {
    0: "lunes", 1: "martes", 2: "miércoles", 3: "jueves",
    4: "viernes", 5: "sábado", 6: "domingo",
}

_MONTHS_ES = {
    1: "enero", 2: "febrero", 3: "marzo", 4: "abril",
    5: "mayo", 6: "junio", 7: "julio", 8: "agosto",
    9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre",
}

# ── Cache TTL per block (seconds) ───────────────────────────────────────
#
# Each block has a different staleness tolerance.  IDENTITY changes very
# rarely (immutable after set), so it can be cached for hours.  The
# conversation-related blocks (ÚLTIMOS 14 DÍAS, EPISODIO ACTUAL) change
# as new sessions arrive, so they use a short TTL.  SEMANTIC MEMORY and
# PATRONES shift more slowly.

_BLOCK_TTLS: Dict[str, int] = {
    "identity":  3600,  # 1 hour  — immutable after creation
    "recent":     300,  # 5 min   — new sessions arrive frequently
    "episode":    300,  # 5 min   — week sessions change often
    "semantic":   600,  # 10 min  — fact store updates are less frequent
    "patterns":   600,  # 10 min  — patterns evolve slowly
}


def _format_date_es(dt: date) -> str:
    """Format a date in Spanish: '12 de abril de 2026'."""
    return f"{dt.day} de {_MONTHS_ES.get(dt.month, str(dt.month))} de {dt.year}"


def _weekday_es(dt: date) -> str:
    return _DAYS_ES.get(dt.weekday(), "")


# ── Data Access Layer ────────────────────────────────────────────────────


class _IdentityDAO:
    """Thin wrapper to read identity.db."""

    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self._db_path))
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def get_identity(self) -> Optional[Dict[str, Any]]:
        try:
            conn = self._get_conn()
            row = conn.execute("SELECT * FROM identity LIMIT 1").fetchone()
            if row is None:
                return None
            d = dict(row)
            # Only return if meaningful data exists
            if not d.get("name") and not d.get("birthday"):
                return None
            return d
        except Exception as e:
            logger.debug("Could not read identity.db: %s", e)
            return None

    def get_days_alive(self) -> Optional[int]:
        ident = self.get_identity()
        if not ident or not ident.get("birthday"):
            return None
        try:
            bday = date.fromisoformat(ident["birthday"])
            return (date.today() - bday).days + 1
        except (ValueError, TypeError):
            return None

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None


class _SessionDAO:
    """Thin wrapper to read state.db sessions/messages."""

    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self._db_path))
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def get_recent_sessions(self, days: int = 14) -> List[Dict[str, Any]]:
        """Get sessions from the last N days."""
        try:
            conn = self._get_conn()
            cutoff = time.time() - (days * 86400)
            rows = conn.execute(
                """SELECT id, source, title, started_at, ended_at,
                          message_count, tool_call_count,
                          input_tokens, output_tokens
                   FROM sessions
                   WHERE started_at >= ?
                   ORDER BY started_at DESC""",
                (cutoff,),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.debug("Could not read sessions: %s", e)
            return []

    def get_sessions_for_week(
        self, week_start: date, week_end: date
    ) -> List[Dict[str, Any]]:
        """Get sessions within a specific week."""
        try:
            conn = self._get_conn()
            start_ts = datetime.combine(week_start, datetime.min.time()).timestamp()
            end_ts = datetime.combine(
                week_end + timedelta(days=1), datetime.min.time()
            ).timestamp()
            rows = conn.execute(
                """SELECT id, source, title, started_at, ended_at,
                          message_count, tool_call_count,
                          input_tokens, output_tokens
                   FROM sessions
                   WHERE started_at >= ? AND started_at < ?
                   ORDER BY started_at""",
                (start_ts, end_ts),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.debug("Could not read week sessions: %s", e)
            return []

    def get_user_messages_sample(
        self, session_id: str, limit: int = 5
    ) -> List[str]:
        """Get a sample of user messages from a session for summarization."""
        try:
            conn = self._get_conn()
            rows = conn.execute(
                """SELECT content FROM messages
                   WHERE session_id = ? AND role = 'user'
                     AND content IS NOT NULL AND content != ''
                   ORDER BY timestamp
                   LIMIT ?""",
                (session_id, limit),
            ).fetchall()
            return [r["content"][:200] for r in rows if r["content"]]
        except Exception as e:
            logger.debug("Could not read messages for %s: %s", session_id, e)
            return []

    def get_all_sessions_stats(self) -> List[Dict[str, Any]]:
        """Get all sessions with basic stats for pattern detection."""
        try:
            conn = self._get_conn()
            rows = conn.execute(
                """SELECT id, source, title, started_at, message_count,
                          tool_call_count, input_tokens, output_tokens
                   FROM sessions
                   ORDER BY started_at"""
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.debug("Could not read session stats: %s", e)
            return []

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None


class _FactsDAO:
    """Read semantic facts from memory_store.db (Holographic) or
    any SQLite-backed facts table."""

    def __init__(self, db_path: Optional[Path] = None):
        self._db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None

    def _get_conn(self) -> Optional[sqlite3.Connection]:
        if self._db_path is None:
            return None
        if self._conn is None:
            try:
                self._conn = sqlite3.connect(str(self._db_path))
                self._conn.row_factory = sqlite3.Row
            except Exception as e:
                logger.debug("Could not open facts DB: %s", e)
                return None
        return self._conn

    def get_facts(
        self, query: str = "", limit: int = 10
    ) -> List[Dict[str, Any]]:
        """Retrieve semantic facts. If a facts table exists, query it."""
        conn = self._get_conn()
        if conn is None:
            return []
        try:
            # Try to find a facts table
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            table_names = {t["name"] for t in tables}

            # Look for common fact table patterns
            fact_table = None
            for candidate in ("facts", "memory_facts", "semantic_facts", "hrr_facts"):
                if candidate in table_names:
                    fact_table = candidate
                    break

            if fact_table is None:
                return []

            # Get column info to understand schema
            pragma = conn.execute(f"PRAGMA table_info({fact_table})").fetchall()
            columns = {p["name"] for p in pragma}

            # Try to select relevant facts
            if "content" in columns:
                rows = conn.execute(
                    f"SELECT * FROM {fact_table} ORDER BY rowid DESC LIMIT ?",
                    (limit,),
                ).fetchall()
                return [dict(r) for r in rows]
            return []
        except Exception as e:
            logger.debug("Could not read facts: %s", e)
            return []

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None


# ── TemporalContextEngine ────────────────────────────────────────────────


class TemporalContextEngine(ContextEngine):
    """Context engine that produces temporal-aware system prompt blocks.

    Extends ContextEngine ABC to satisfy the plugin registration contract.
    Compression is delegated to the built-in ContextCompressor — this engine
    focuses on building temporal context blocks via ``build()``.

    The ``build()`` method assembles five context blocks:

    1. IDENTIDAD — name, day N, email, personality
    2. ÚLTIMOS 14 DÍAS — recent conversation index
    3. EPISODIO ACTUAL — current week narrative
    4. MEMORIA SEMÁNTICA — relevant facts
    5. PATRONES DETECTADOS — behavioral patterns

    Accepts ``user_id`` or ``context_id`` to scope the context to a user.

    Does NOT implement compression itself — that is handled by the standard
    ContextCompressor. This engine is a context *builder*, not a compressor.
    """

    def __init__(
        self,
        hermes_home: str | Path | None = None,
        user_id: str | None = None,
        context_id: str | None = None,
    ):
        from hermes_constants import get_hermes_home

        self._hermes_home = Path(hermes_home or get_hermes_home())
        self._user_id = user_id or context_id or "default"

        # Resolve DB paths
        self._identity_db = self._hermes_home / "identity.db"
        self._state_db = self._hermes_home / "state.db"
        self._memory_db = self._hermes_home / "memory.db"

        # Try memory_store.db for semantic facts (Holographic)
        self._memory_store_db: Optional[Path] = None
        for candidate in (
            self._hermes_home / "memory_store.db",
            self._hermes_home / "holographic.db",
        ):
            if candidate.exists():
                self._memory_store_db = candidate
                break

        # DAOs (lazy init)
        self._identity_dao: Optional[_IdentityDAO] = None
        self._session_dao: Optional[_SessionDAO] = None
        self._facts_dao: Optional[_FactsDAO] = None

        # ── Cache state ────────────────────────────────────────────────
        # _cache[user_id][block_name] = (content: str | None, ts: float)
        # All reads/writes go through _cache_lock for thread safety.
        self._cache: Dict[str, Dict[str, Tuple[Optional[str], float]]] = {}
        self._cache_lock = threading.Lock()

    # -- ContextEngine ABC implementation ---------------------------------

    @property
    def name(self) -> str:
        """Short identifier for this engine."""
        return "temporal"

    def update_from_response(self, usage: Dict[str, Any]) -> None:
        """Track token usage from API response.

        Delegates to standard token tracking fields inherited from
        ContextEngine. No-op beyond that — compression is not this
        engine's responsibility.
        """
        self.last_prompt_tokens = usage.get("prompt_tokens", 0)
        self.last_completion_tokens = usage.get("completion_tokens", 0)
        self.last_total_tokens = usage.get("total_tokens", 0)

    def should_compress(self, prompt_tokens: int = None) -> bool:
        """Always returns False — compression is handled externally.

        This engine only builds temporal context; it does not perform
        its own compaction. The system should use ContextCompressor
        for compression.
        """
        return False

    def compress(
        self,
        messages: List[Dict[str, Any]],
        current_tokens: int = None,
        focus_topic: str = None,
    ) -> List[Dict[str, Any]]:
        """No-op — returns messages unchanged.

        Compression is not this engine's responsibility. Use the
        built-in ContextCompressor for compaction.
        """
        return messages

    # -- Cache helpers ----------------------------------------------------

    def _get_cached_block(self, user_id: str, block_name: str) -> Optional[str]:
        """Return cached content for *block_name* if still fresh, else ``None``.

        Thread-safe: acquires ``_cache_lock`` for the read.
        """
        ttl = _BLOCK_TTLS.get(block_name, 300)
        with self._cache_lock:
            entry = self._cache.get(user_id, {}).get(block_name)
            if entry is None:
                return None
            content, ts = entry
            if (time.time() - ts) < ttl:
                return content
            # Stale — remove so next set starts clean
            self._cache.get(user_id, {}).pop(block_name, None)
            return None

    def _set_cached_block(
        self, user_id: str, block_name: str, content: Optional[str]
    ) -> None:
        """Store *content* in the cache with a fresh timestamp.

        Thread-safe: acquires ``_cache_lock`` for the write.
        """
        with self._cache_lock:
            if user_id not in self._cache:
                self._cache[user_id] = {}
            self._cache[user_id][block_name] = (content, time.time())

    def invalidate(self, user_id: str, block: str | None = None) -> None:
        """Invalidate cached context for *user_id*.

        Args:
            user_id: The user whose cache entries should be cleared.
            block:   If given, only this block is invalidated (e.g.
                     ``"recent"``).  If ``None``, **all** blocks for the
                     user are cleared.

        External callers (message store, fact store) should invoke this
        when new data arrives so the next ``build()`` regenerates the
         affected blocks.
        """
        with self._cache_lock:
            if user_id not in self._cache:
                return
            if block is None:
                del self._cache[user_id]
            else:
                self._cache[user_id].pop(block, None)

    # -- DAO access (lazy) ------------------------------------------------

    @property
    def identity_dao(self) -> _IdentityDAO:
        if self._identity_dao is None:
            self._identity_dao = _IdentityDAO(self._identity_db)
        return self._identity_dao

    @property
    def session_dao(self) -> _SessionDAO:
        if self._session_dao is None:
            self._session_dao = _SessionDAO(self._state_db)
        return self._session_dao

    @property
    def facts_dao(self) -> _FactsDAO:
        if self._facts_dao is None:
            self._facts_dao = _FactsDAO(self._memory_store_db)
        return self._facts_dao

    # -- Main entry point -------------------------------------------------

    def build(
        self,
        user_message: str = "",
        context_id: str | None = None,
    ) -> str:
        """Build the full temporal context string with per-block caching.

        Each block (IDENTITY, ÚLTIMOS 14 DÍAS, EPISODIO ACTUAL, MEMORIA
        SEMÁNTICA, PATRONES) is cached independently with its own TTL.
        Stale blocks are regenerated on demand; fresh blocks are served
        from cache (partial cache hit).  Callers never need to manage
        the cache — this method always returns fresh-enough context.

        Args:
            user_message: The current user message, used for semantic
                fact relevance filtering.
            context_id: Optional override for the user context scope.

        Returns:
            A plain-text string with five ``## SECTION`` blocks separated
            by blank lines. Empty sections are omitted.
        """
        if context_id:
            self._user_id = context_id

        uid = self._user_id
        blocks: List[str] = []

        # Map: (cache_key, builder_callable, builder_kwargs)
        block_specs: List[Tuple[str, Any, dict]] = [
            ("identity",  self._build_identity,          {}),
            ("recent",    lambda: self._build_recent_index(days=14), {}),
            ("episode",   self._build_current_week_episode, {}),
            ("semantic",  lambda: self._build_semantic_memory(query=user_message), {}),
            ("patterns",  self._build_patterns,          {}),
        ]

        for cache_key, builder, kwargs in block_specs:
            cached = self._get_cached_block(uid, cache_key)
            if cached is not None:
                # Cache hit — use stored content (None means "no data")
                if cached:  # skip empty-string sentinel
                    blocks.append(cached)
                continue

            # Cache miss — regenerate
            content = builder(**kwargs) if kwargs else builder()
            self._set_cached_block(uid, cache_key, content or "")
            if content:
                blocks.append(content)

        return "\n\n".join(blocks)

    def build_progressive(
        self,
        user_message: str = "",
        context_id: str | None = None,
    ) -> str:
        """Alias for ``build()`` — same caching semantics.

        Provided so callers that expect a progressive-build API can
        use this engine without modification.  Identical to ``build()``
        in every respect.
        """
        return self.build(user_message=user_message, context_id=context_id)

    # -- Block builders ---------------------------------------------------

    def _build_identity(self) -> Optional[str]:
        """IDENTIDAD block: name, day N, email, personality."""
        identity = self.identity_dao.get_identity()
        if not identity:
            return None

        name = identity.get("name", "")
        display_name = identity.get("display_name", "")
        birthday = identity.get("birthday", "")
        email = identity.get("email", "")
        personality = identity.get("personality", "")

        if not name and not birthday:
            return None

        lines = ["═══ IDENTIDAD ═══"]

        if name:
            if display_name and display_name != name:
                lines.append(f'Nombre: {name} ("{display_name}")')
            else:
                lines.append(f"Nombre: {name}")

        if birthday:
            try:
                bday = date.fromisoformat(birthday)
                lines.append(f"Cumpleaños: {_format_date_es(bday)}")
            except ValueError:
                lines.append(f"Cumpleaños: {birthday}")

        days = self.identity_dao.get_days_alive()
        if days is not None:
            lines.append(f"Edad: Día {days}")

        if email:
            lines.append(f"Email: {email}")

        if personality:
            lines.append(f"Personalidad: {personality}")

        return "\n".join(lines)

    def _build_recent_index(self, days: int = 14) -> Optional[str]:
        """ÚLTIMOS 14 DÍAS block: index of recent conversations."""
        sessions = self.session_dao.get_recent_sessions(days=days)
        if not sessions:
            return None

        lines = [f"═══ ÚLTIMOS {days} DÍAS ═══"]

        birthday = None
        try:
            identity = self.identity_dao.get_identity()
            if identity and identity.get("birthday"):
                birthday = date.fromisoformat(identity["birthday"])
        except (ValueError, TypeError):
            pass

        for s in sessions:
            started = s.get("started_at")
            if not started:
                continue

            dt = datetime.fromtimestamp(started)
            day_str = _weekday_es(dt.date())
            date_str = dt.strftime("%Y-%m-%d")
            title = s.get("title") or s.get("source") or "sesión"
            msg_count = s.get("message_count", 0)

            # Calculate day number from birthday
            day_num_str = ""
            if birthday:
                day_num = (dt.date() - birthday).days + 1
                if day_num > 0:
                    day_num_str = f"[Día {day_num}] "

            # Compact summary line
            summary_parts = [f"{day_num_str}{date_str} ({day_str})"]
            summary_parts.append(f"— {title}")
            if msg_count:
                summary_parts.append(f"({msg_count} msgs)")

            lines.append("  • " + " ".join(summary_parts))

        return "\n".join(lines)

    def _build_current_week_episode(self) -> Optional[str]:
        """EPISODIO ACTUAL block: narrative of the current week."""
        today = date.today()
        # Monday of this week
        week_start = today - timedelta(days=today.weekday())
        week_end = week_start + timedelta(days=6)

        sessions = self.session_dao.get_sessions_for_week(week_start, week_end)
        if not sessions:
            return None

        # Calculate day range from birthday
        birthday = None
        day_range_str = ""
        try:
            identity = self.identity_dao.get_identity()
            if identity and identity.get("birthday"):
                birthday = date.fromisoformat(identity["birthday"])
                d1 = (week_start - birthday).days + 1
                d2 = (week_end - birthday).days + 1
                if d1 > 0:
                    day_range_str = f" (Día {d1}-{d2})"
        except (ValueError, TypeError):
            pass

        lines = [f"═══ ESTA SEMANA{day_range_str} ═══"]

        # Gather session titles and sources
        titles = []
        sources = Counter()
        total_msgs = 0
        for s in sessions:
            t = s.get("title") or s.get("source") or ""
            if t:
                titles.append(t)
            src = s.get("source", "unknown")
            sources[src] += 1
            total_msgs += s.get("message_count", 0)

        # Build a brief narrative
        n_sessions = len(sessions)
        week_label = f"{week_start.strftime('%d %b')} - {week_end.strftime('%d %b %Y')}"
        lines.append(f"Semana del {week_label}")
        lines.append(
            f"Sesiones: {n_sessions} | Mensajes totales: {total_msgs}"
        )

        if titles:
            # Show up to 5 session topics
            unique_titles = list(dict.fromkeys(titles))[:5]
            lines.append("Temas: " + ", ".join(unique_titles))

        # Day-by-day breakdown
        day_sessions: Dict[str, List[str]] = defaultdict(list)
        for s in sessions:
            started = s.get("started_at")
            if not started:
                continue
            dt = datetime.fromtimestamp(started)
            day_label = _weekday_es(dt.date())
            title = s.get("title") or s.get("source") or "sesión"
            day_sessions[day_label].append(title)

        if day_sessions:
            lines.append("")
            for day_name in ["lunes", "martes", "miércoles", "jueves",
                             "viernes", "sábado", "domingo"]:
                if day_name in day_sessions:
                    for t in day_sessions[day_name]:
                        lines.append(f"  {day_name.capitalize()}: {t}")

        return "\n".join(lines)

    def _build_semantic_memory(
        self, query: str = "", limit: int = 8
    ) -> Optional[str]:
        """MEMORIA SEMÁNTICA block: relevant facts from memory store."""
        facts = self.facts_dao.get_facts(query=query, limit=limit)
        if not facts:
            return None

        lines = ["═══ MEMORIA SEMÁNTICA ═══"]

        for fact in facts:
            # Adapt to different fact schema shapes
            content = (
                fact.get("content")
                or fact.get("text")
                or fact.get("fact")
                or ""
            )
            if not content:
                continue

            # Truncate very long facts
            if len(content) > 200:
                content = content[:197] + "..."

            category = fact.get("category", "")
            if category:
                lines.append(f"  • [{category}] {content}")
            else:
                lines.append(f"  • {content}")

        if len(lines) == 1:
            return None

        return "\n".join(lines)

    def _build_patterns(self, min_sessions: int = 3) -> Optional[str]:
        """PATRONES DETECTADOS block: behavioral patterns."""
        all_sessions = self.session_dao.get_all_sessions_stats()
        if len(all_sessions) < min_sessions:
            return None

        lines = ["═══ PATRONES DETECTADOS ═══"]
        pattern_found = False

        # 1. Activity time patterns
        hour_counts: Counter = Counter()
        weekday_counts: Counter = Counter()
        for s in all_sessions:
            started = s.get("started_at")
            if not started:
                continue
            dt = datetime.fromtimestamp(started)
            hour_counts[dt.hour] += 1
            weekday_counts[_weekday_es(dt.date())] += 1

        if hour_counts:
            peak_hours = hour_counts.most_common(3)
            hour_range = ", ".join(
                f"{h:02d}:00 ({c})" for h, c in peak_hours
            )
            lines.append(f"  • Horarios de actividad: {hour_range}")
            pattern_found = True

        if weekday_counts:
            top_days = weekday_counts.most_common(3)
            day_str = ", ".join(f"{d} ({c})" for d, c in top_days)
            lines.append(f"  • Días más activos: {day_str}")
            pattern_found = True

        # 2. Source/platform usage
        source_counts: Counter = Counter()
        for s in all_sessions:
            src = s.get("source", "unknown")
            source_counts[src] += 1

        if source_counts:
            top_sources = source_counts.most_common(3)
            src_str = ", ".join(f"{s} ({c})" for s, c in top_sources)
            lines.append(f"  • Plataformas: {src_str}")
            pattern_found = True

        # 3. Session intensity (avg messages per session)
        msg_counts = [s.get("message_count", 0) for s in all_sessions]
        if msg_counts:
            avg_msgs = sum(msg_counts) / len(msg_counts)
            lines.append(
                f"  • Intensidad promedio: {avg_msgs:.0f} mensajes/sesión"
            )
            pattern_found = True

        # 4. Tool usage patterns
        tool_counts = [s.get("tool_call_count", 0) for s in all_sessions]
        if tool_counts:
            total_tools = sum(tool_counts)
            avg_tools = total_tools / len(tool_counts) if tool_counts else 0
            if total_tools > 0:
                lines.append(
                    f"  • Uso de herramientas: {avg_tools:.1f} llamadas/sesión "
                    f"(total: {total_tools})"
                )
                pattern_found = True

        if not pattern_found:
            return None

        return "\n".join(lines)

    # -- Cleanup ----------------------------------------------------------

    def close(self):
        """Close all database connections."""
        if self._identity_dao:
            self._identity_dao.close()
        if self._session_dao:
            self._session_dao.close()
        if self._facts_dao:
            self._facts_dao.close()


# ── Plugin Registration ──────────────────────────────────────────────────


def register(ctx) -> None:
    """Register TemporalContextEngine as a context engine plugin.

    This function is called by the plugin discovery system. It registers
    a factory that creates the engine with the current hermes_home.
    """
    engine = TemporalContextEngine()
    ctx.register_context_engine(engine)


# Convenience: direct instantiation for testing
def create_engine(
    hermes_home: str | Path | None = None,
    user_id: str | None = None,
) -> TemporalContextEngine:
    """Create a TemporalContextEngine instance (for testing / standalone use)."""
    return TemporalContextEngine(hermes_home=hermes_home, user_id=user_id)
