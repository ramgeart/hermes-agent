"""DreamProcessor — incremental session analysis and metadata extraction.

Scans Hermes sessions, extracts structured metadata (title, summary, tags,
mood, importance) via LLM, identifies semantic facts, and persists results
as ``dream.json`` per session.  Uses a checkpoint file for incremental
processing — only sessions whose fingerprint changed are re-analyzed.

Architecture:
    - ``dream_checkpoint.json`` — per-session fingerprint cache
    - ``<session_dir>/dream.json`` — extracted metadata per session
    - LLM call for semantic extraction (title, summary, tags, mood, facts)

Usage:
    from agent.dream_processor import DreamProcessor

    dp = DreamProcessor(
        state_db_path="/home/user/.hermes/state.db",
        identity_db_path="/home/user/.hermes/identity.db",
        memory_db_path="/home/user/.hermes/state.db",
        checkpoint_path="/home/user/.hermes/dream_checkpoint.json",
    )
    result = dp.run()
    # result = {"processed": 5, "skipped": 12, "facts_extracted": [...]}
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# -- Constants -------------------------------------------------------------

TTL_CACHE_SECONDS = 300  # 5 minutes — skip sessions processed within TTL

MOOD_OPTIONS = [
    "exploratory", "focused", "frustrated", "celebratory",
    "analytical", "creative", "routine", "urgent", "reflective",
]

FACT_TYPES = [
    "decision", "preference", "entity", "constraint",
    "workflow", "preference_change", "insight", "correction",
]

# -- Session fingerprint (adapted from hermes-achievements) ----------------


def session_fingerprint(meta: Dict[str, Any]) -> Dict[str, Any]:
    """Build a fingerprint dict from session metadata.

    The fingerprint captures the essential state that would invalidate
    a cached analysis.  If this dict matches the checkpoint, the session
    can be skipped.
    """
    return {
        "started_at": meta.get("started_at"),
        "last_active": meta.get("last_active"),
        "message_count": meta.get("message_count", 0),
        "title": meta.get("title") or meta.get("preview") or "Untitled",
    }


def fingerprint_hash(fp: Dict[str, Any]) -> str:
    """Deterministic hash of a fingerprint dict."""
    blob = json.dumps(fp, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


# -- Checkpoint management -------------------------------------------------


class DreamCheckpoint:
    """Per-session fingerprint cache for incremental scanning.

    Stores ``{session_id: {fingerprint: {...}, processed_at: timestamp}}``
    so only changed sessions are re-processed.
    """

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._data: Dict[str, Any] = self._load()

    def _load(self) -> Dict[str, Any]:
        if not self._path.exists():
            return {"schema_version": 1, "generated_at": 0, "sessions": {}}
        try:
            data = json.loads(self._path.read_text())
            if isinstance(data, dict):
                data.setdefault("schema_version", 1)
                data.setdefault("generated_at", 0)
                data.setdefault("sessions", {})
                if isinstance(data.get("sessions"), dict):
                    return data
        except Exception:
            logger.warning("Corrupt checkpoint at %s, resetting", self._path)
        return {"schema_version": 1, "generated_at": 0, "sessions": {}}

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._data["generated_at"] = int(time.time())
        self._path.write_text(json.dumps(self._data, indent=2, sort_keys=True))

    def needs_processing(self, session_id: str, fp: Dict[str, Any]) -> bool:
        """Check if a session needs (re)processing.

        Returns True if:
        - Session never seen, OR
        - Fingerprint changed, OR
        - TTL expired (session was processed > TTL_CACHE_SECONDS ago)
        """
        cached = self._data["sessions"].get(session_id)
        if not isinstance(cached, dict):
            return True
        if cached.get("fingerprint") != fp:
            return True
        processed_at = cached.get("processed_at", 0)
        if (time.time() - processed_at) > TTL_CACHE_SECONDS:
            return True
        return False

    def mark_processed(self, session_id: str, fp: Dict[str, Any]) -> None:
        self._data["sessions"][session_id] = {
            "fingerprint": fp,
            "processed_at": int(time.time()),
        }

    @property
    def session_count(self) -> int:
        return len(self._data.get("sessions", {}))


# -- LLM extraction -------------------------------------------------------


def _build_extraction_prompt(messages: List[Dict[str, Any]], birthday: str) -> str:
    """Build the LLM prompt for session analysis.

    Only includes user and assistant messages (no tool calls) to keep
    the prompt focused on semantic content.
    """
    conversation_parts = []
    for msg in messages:
        role = msg.get("role", "unknown")
        if role not in ("user", "assistant"):
            continue
        content = msg.get("content", "")
        if not content:
            continue
        # Truncate very long messages to keep prompt manageable
        if isinstance(content, str) and len(content) > 2000:
            content = content[:2000] + "... [truncated]"
        conversation_parts.append(f"[{role}]: {content}")

    conversation_text = "\n".join(conversation_parts[-50:])  # Last 50 messages max

    mood_list = ", ".join(MOOD_OPTIONS)
    fact_types = ", ".join(FACT_TYPES)

    return f"""Analyze this conversation and extract structured metadata.

CONVERSATION:
{conversation_text}

Respond in EXACTLY this JSON format (no other text):
{{
  "title": "concise descriptive title, max 60 chars",
  "summary": "2-3 sentence overview of what happened in this conversation",
  "tags": ["tag1", "tag2", "tag3"],
  "mood": "one of: {mood_list}",
  "importance": 3,
  "facts": [
    {{
      "fact_type": "one of: {fact_types}",
      "content": "concrete fact extracted from the conversation",
      "confidence": 0.8
    }}
  ]
}}

RULES:
- title: short, descriptive, captures the main activity
- summary: 2-3 sentences, captures key actions and outcomes
- tags: 2-8 relevant topic tags (lowercase, no spaces)
- mood: single word from the allowed list
- importance: 1 (trivial) to 5 (critical life event)
- facts: only concrete, durable facts (decisions, preferences, entities)
  - confidence: 0.0-1.0, how certain you are this is a stable fact
  - Do NOT include ephemeral/transient details as facts
  - Each fact should be self-contained (understandable without context)
"""


def _parse_llm_response(raw: str) -> Optional[Dict[str, Any]]:
    """Parse LLM JSON response, handling common formatting issues."""
    if not raw:
        return None

    # Try direct JSON parse
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Try extracting JSON from markdown code block
    import re
    json_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", raw, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group(1))
        except json.JSONDecodeError:
            pass

    # Try finding first { to last }
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            pass

    logger.warning("Failed to parse LLM response: %s...", raw[:200])
    return None


def _validate_extraction(data: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and normalize extracted fields."""
    result = {
        "title": str(data.get("title", "Untitled"))[:80],
        "summary": str(data.get("summary", ""))[:500],
        "tags": [],
        "mood": "routine",
        "importance": 3,
        "facts": [],
    }

    # Tags: list of strings
    tags = data.get("tags", [])
    if isinstance(tags, list):
        result["tags"] = [str(t).strip().lower()[:30] for t in tags if t][:8]
    elif isinstance(tags, str):
        result["tags"] = [t.strip().lower()[:30] for t in tags.split(",") if t.strip()][:8]

    # Mood: validate against options
    mood = str(data.get("mood", "routine")).lower().strip()
    if mood in MOOD_OPTIONS:
        result["mood"] = mood

    # Importance: clamp 1-5
    try:
        imp = int(data.get("importance", 3))
        result["importance"] = max(1, min(5, imp))
    except (ValueError, TypeError):
        result["importance"] = 3

    # Facts: validate structure
    raw_facts = data.get("facts", [])
    if isinstance(raw_facts, list):
        for fact in raw_facts:
            if not isinstance(fact, dict):
                continue
            fact_type = str(fact.get("fact_type", "insight")).lower()
            if fact_type not in FACT_TYPES:
                fact_type = "insight"
            content = str(fact.get("content", "")).strip()
            if not content:
                continue
            try:
                confidence = float(fact.get("confidence", 0.5))
                confidence = max(0.0, min(1.0, confidence))
            except (ValueError, TypeError):
                confidence = 0.5

            result["facts"].append({
                "fact_type": fact_type,
                "content": content[:500],
                "confidence": confidence,
            })

    return result


# -- DreamProcessor --------------------------------------------------------


class DreamProcessor:
    """Core dreaming engine — incremental session analysis and fact extraction.

    Scans Hermes sessions from state.db, compares fingerprints against
    a checkpoint file, and processes only new/modified sessions.  For each
    session, calls an LLM to extract structured metadata and semantic facts.

    Args:
        state_db_path: Path to Hermes state.db (sessions + messages)
        identity_db_path: Path to identity.db (birthday for day_number)
        memory_db_path: Path to memory.db (conversation_index)
        checkpoint_path: Path to dream_checkpoint.json
        llm_caller: Callable[[str], str] that sends a prompt to the LLM
            and returns the response text.  If None, uses a stub that
            returns a default response.
        dream_data_dir: Base directory for dream.json files.  If None,
            uses ~/.hermes/dream_data/
    """

    def __init__(
        self,
        state_db_path: str,
        identity_db_path: str,
        memory_db_path: str,
        checkpoint_path: str,
        llm_caller: Optional[Callable[[str], str]] = None,
        dream_data_dir: Optional[str] = None,
    ):
        self._state_db_path = state_db_path
        self._identity_db_path = identity_db_path
        # memory_db_path defaults to state_db_path so conversation_index
        # shares the same DB as sessions (enabling FK enforcement).
        # Callers can pass memory_db_path=state_db_path explicitly, or
        # omit it (passing state_db_path) for the unified layout.
        self._memory_db_path = memory_db_path or state_db_path
        self._checkpoint = DreamCheckpoint(checkpoint_path)
        self._llm_caller = llm_caller or self._stub_llm
        self._dream_data_dir = Path(dream_data_dir) if dream_data_dir else None

    @staticmethod
    def _stub_llm(prompt: str) -> str:
        """Stub LLM for testing — returns a minimal valid response."""
        return json.dumps({
            "title": "Stub session",
            "summary": "This is a stub summary for testing purposes.",
            "tags": ["test", "stub"],
            "mood": "routine",
            "importance": 1,
            "facts": [],
        })

    # -- Public API --------------------------------------------------------

    def run(self, force: bool = False) -> Dict[str, Any]:
        """Run the full dream pipeline.

        Args:
            force: If True, reprocess ALL sessions regardless of checkpoint.

        Returns:
            Dict with keys:
                - processed: number of sessions processed this run
                - skipped: number of sessions skipped (unchanged)
                - total_seen: total sessions found in state.db
                - facts_extracted: list of all extracted facts with source_session
                - errors: list of {session_id, error} for failed sessions
        """
        result = {
            "processed": 0,
            "skipped": 0,
            "total_seen": 0,
            "facts_extracted": [],
            "errors": [],
        }

        # 1. Get identity for day_number calculation
        birthday = self._get_birthday()
        if not birthday:
            logger.error("No birthday found in identity.db — cannot compute day_number")
            return result

        # 2. Scan sessions
        sessions_meta = self._scan_sessions()
        result["total_seen"] = len(sessions_meta)

        if not sessions_meta:
            logger.info("No sessions found in state.db")
            self._checkpoint.save()
            return result

        # 3. Process each session
        for meta in sessions_meta:
            session_id = meta.get("id")
            if not session_id:
                continue

            fp = session_fingerprint(meta)

            if not force and not self._checkpoint.needs_processing(session_id, fp):
                result["skipped"] += 1
                continue

            try:
                session_result = self._process_session(session_id, meta, birthday)
                self._checkpoint.mark_processed(session_id, fp)
                result["processed"] += 1

                # Accumulate facts
                for fact in session_result.get("facts", []):
                    fact["source_session"] = session_id
                    result["facts_extracted"].append(fact)

            except Exception as e:
                logger.error("Failed to process session %s: %s", session_id, e)
                result["errors"].append({"session_id": session_id, "error": str(e)})

        # 4. Persist checkpoint
        self._checkpoint.save()

        # 5. Persist to memory.db conversation_index
        self._persist_to_memory_index()

        logger.info(
            "Dream run complete: processed=%d, skipped=%d, total=%d, facts=%d",
            result["processed"], result["skipped"],
            result["total_seen"], len(result["facts_extracted"]),
        )
        return result

    def get_session_dream(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Load the dream.json for a specific session, if it exists."""
        dream_path = self._dream_path(session_id)
        if dream_path.exists():
            try:
                return json.loads(dream_path.read_text())
            except Exception:
                return None
        return None

    @property
    def checkpoint_session_count(self) -> int:
        return self._checkpoint.session_count

    # -- Internal ----------------------------------------------------------

    def _get_birthday(self) -> Optional[str]:
        """Read birthday from identity.db."""
        try:
            conn = sqlite3.connect(self._identity_db_path)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT birthday FROM identity WHERE birthday IS NOT NULL AND birthday != '' LIMIT 1"
            ).fetchone()
            conn.close()
            return row["birthday"] if row else None
        except Exception as e:
            logger.error("Failed to read birthday from identity.db: %s", e)
            return None

    def _scan_sessions(self) -> List[Dict[str, Any]]:
        """List all sessions from state.db with metadata.

        Returns list of dicts with keys: id, title, started_at, last_active,
        message_count, preview, source, model.
        """
        try:
            conn = sqlite3.connect(self._state_db_path)
            conn.row_factory = sqlite3.Row

            rows = conn.execute("""
                SELECT
                    s.id,
                    s.title,
                    s.source,
                    s.model,
                    s.started_at,
                    s.ended_at,
                    (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) as message_count,
                    (SELECT m.content FROM messages m
                     WHERE m.session_id = s.id AND m.role = 'user'
                     ORDER BY m.id LIMIT 1) as preview,
                    (SELECT MAX(m.timestamp) FROM messages m
                     WHERE m.session_id = s.id) as last_active
                FROM sessions s
                WHERE s.parent_session_id IS NULL
                ORDER BY s.started_at
            """).fetchall()

            conn.close()
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error("Failed to scan sessions from state.db: %s", e)
            return []

    def _get_session_messages(self, session_id: str) -> List[Dict[str, Any]]:
        """Load all messages for a session."""
        try:
            conn = sqlite3.connect(self._state_db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY id",
                (session_id,),
            ).fetchall()
            conn.close()
            msgs = []
            for row in rows:
                msg = dict(row)
                if msg.get("tool_calls"):
                    try:
                        msg["tool_calls"] = json.loads(msg["tool_calls"])
                    except (json.JSONDecodeError, TypeError):
                        msg["tool_calls"] = []
                msgs.append(msg)
            return msgs
        except Exception as e:
            logger.error("Failed to get messages for session %s: %s", session_id, e)
            return []

    def _process_session(
        self,
        session_id: str,
        meta: Dict[str, Any],
        birthday: str,
    ) -> Dict[str, Any]:
        """Process a single session: extract metadata and facts via LLM."""
        messages = self._get_session_messages(session_id)
        if not messages:
            logger.debug("Session %s has no messages, skipping", session_id)
            return {"facts": []}

        # Build and send prompt to LLM
        prompt = _build_extraction_prompt(messages, birthday)
        raw_response = self._llm_caller(prompt)

        # Parse response
        parsed = _parse_llm_response(raw_response)
        if not parsed:
            logger.warning("LLM returned unparseable response for session %s", session_id)
            parsed = {
                "title": meta.get("title") or meta.get("preview") or "Untitled",
                "summary": "",
                "tags": [],
                "mood": "routine",
                "importance": 1,
                "facts": [],
            }

        # Validate fields
        extraction = _validate_extraction(parsed)

        # Compute day_number
        try:
            session_dt = datetime.fromisoformat(str(meta.get("started_at", "")))
            birthday_dt = datetime.fromisoformat(birthday)
            day_number = (session_dt.date() - birthday_dt.date()).days + 1
        except (ValueError, TypeError):
            day_number = 0

        # Compute weekday
        try:
            weekday = session_dt.strftime("%A")
        except Exception:
            weekday = ""

        # Build dream data
        dream_data = {
            "session_id": session_id,
            "title": extraction["title"],
            "summary": extraction["summary"],
            "tags": extraction["tags"],
            "mood": extraction["mood"],
            "importance": extraction["importance"],
            "day_number": day_number,
            "weekday": weekday,
            "date": str(meta.get("started_at", "")),
            "source": meta.get("source", ""),
            "model": meta.get("model", ""),
            "message_count": meta.get("message_count", 0),
            "facts": extraction["facts"],
            "processed_at": int(time.time()),
        }

        # Persist dream.json
        self._save_dream(session_id, dream_data)

        # Also store in memory.db conversation_index
        self._upsert_conversation_index(dream_data)

        logger.info(
            "Processed session %s: day %d, %d tags, %d facts, mood=%s, importance=%d",
            session_id, day_number, len(extraction["tags"]),
            len(extraction["facts"]), extraction["mood"], extraction["importance"],
        )

        return dream_data

    def _dream_path(self, session_id: str) -> Path:
        """Get the path to a session's dream.json file."""
        if self._dream_data_dir:
            return self._dream_data_dir / session_id / "dream.json"
        # Default: store alongside state.db in dream_data/<session_id>/
        base = Path(self._state_db_path).parent / "dream_data" / session_id
        return base / "dream.json"

    def _save_dream(self, session_id: str, data: Dict[str, Any]) -> None:
        """Persist dream.json for a session."""
        path = self._dream_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, sort_keys=False, default=str))

    def _upsert_conversation_index(self, dream_data: Dict[str, Any]) -> None:
        """Insert or update the conversation_index table via ConversationIndex.

        Uses the canonical schema columns (session_id, title, summary, topics,
        day_number, date, importance, mood) — no extra columns like weekday,
        msg_count, or consolidated that existed in the legacy memory_schema.
        """
        try:
            from agent.conversation_index import ConversationIndex
            ci = ConversationIndex(self._memory_db_path)
            ci.initialize()  # no-op if table already exists
            ci.upsert_conversation(
                session_id=dream_data["session_id"],
                title=dream_data.get("title", ""),
                summary=dream_data.get("summary", ""),
                topics=",".join(dream_data.get("tags", [])),
                day_number=dream_data.get("day_number", 0),
                date=dream_data.get("date", ""),
                importance=dream_data.get("importance", 5),
                mood=dream_data.get("mood", "neutral"),
            )
            ci.close()
        except Exception as e:
            logger.error("Failed to upsert conversation_index for %s: %s",
                         dream_data.get("session_id", "?"), e)

    def _persist_to_memory_index(self) -> None:
        """Ensure all processed sessions are in conversation_index.

        This is a catch-all: if a session has a dream.json but is missing
        from conversation_index, upsert it.
        """
        if not self._dream_data_dir and not Path(self._state_db_path).parent.joinpath("dream_data").exists():
            return

        base = self._dream_data_dir or Path(self._state_db_path).parent / "dream_data"
        if not base.exists():
            return

        try:
            for session_dir in base.iterdir():
                dream_path = session_dir / "dream.json"
                if dream_path.exists():
                    try:
                        data = json.loads(dream_path.read_text())
                        self._upsert_conversation_index(data)
                    except Exception:
                        continue
        except Exception as e:
            logger.warning("Error during memory index persistence sweep: %s", e)
