"""ConsolidationEngine — aggregates per-session dream data into higher-order
summaries and detects cross-session patterns.

This module sits on top of DreamProcessor (per-session extraction) and
EpisodicMemory/EpisodicMemoryManager (DB storage).  It adds:

1. **Weekly episode consolidation** — group processed sessions by ISO week,
   generate LLM narrative, compute aggregates, persist to DB and JSON files.
2. **Monthly and yearly summary updates** — synthesize from weekly/monthly
   summaries (not raw sessions) via LLM or deterministic fallback.
3. **Cross-session pattern detection** — analyze last N sessions for
   recurring themes, repeated tags, evolving topics, mood trends.
4. **Semantic memory fact push** — deduplicate and push extracted facts
   to the semantic memory store.

Usage::

    from agent.consolidation_engine import ConsolidationEngine

    engine = ConsolidationEngine(
        state_db_path="~/.hermes/state.db",
        memory_db_path="~/.hermes/memory.db",
        dream_data_dir="~/.hermes/dream_data",
        output_dir="~/.hermes/dream_consolidations",
    )
    result = engine.run()
    # result = {
    #   "weekly_consolidated": 2,
    #   "monthly_consolidated": 1,
    #   "yearly_consolidated": 0,
    #   "patterns_detected": 3,
    #   "facts_pushed": 5,
    # }
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# -- Constants ---------------------------------------------------------------

DEFAULT_PATTERN_WINDOW = 30  # last N sessions for pattern detection
DEFAULT_PATTERN_CONFIDENCE_THRESHOLD = 0.5


# -- LLM prompt builders ----------------------------------------------------


def _build_weekly_narrative_prompt(
    sessions: List[Dict[str, Any]], week_start: str, week_end: str
) -> str:
    """Build LLM prompt for weekly narrative generation."""
    session_lines = []
    for s in sessions:
        title = s.get("title", "Untitled")
        summary = s.get("summary", "")
        tags = ", ".join(s.get("tags", []))
        mood = s.get("mood", "unknown")
        importance = s.get("importance", 3)
        session_lines.append(
            f"- [{mood}] {title} (importance: {importance}, tags: {tags})\n  {summary}"
        )

    sessions_text = "\n".join(session_lines)
    return f"""Analyze these sessions from the week of {week_start} to {week_end} and
generate a weekly narrative summary.

SESSIONS:
{sessions_text}

Respond in EXACTLY this JSON format (no other text):
{{
  "title": "concise weekly title, max 60 chars",
  "narrative": "3-5 sentence narrative telling the story of this week — key themes, progress, blockers, mood arc",
  "topics": ["top1", "top2", "top3"],
  "key_decisions": ["decision1", "decision2"],
  "mood_arc": "one sentence describing emotional trajectory of the week"
}}

RULES:
- title: captures the dominant theme of the week
- narrative: tells a coherent story, not just a list
- topics: 3-7 dominant topic tags (lowercase, no spaces)
- key_decisions: only important decisions that had lasting impact
- mood_arc: how the emotional tone evolved (e.g. "started frustrated, ended celebratory")
"""


def _build_monthly_synthesis_prompt(
    weekly_episodes: List[Dict[str, Any]], year: int, month: int
) -> str:
    """Build LLM prompt for monthly summary from weekly summaries."""
    month_names = [
        "", "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    ]
    month_name = month_names[month] if 1 <= month <= 12 else str(month)

    episode_lines = []
    for ep in weekly_episodes:
        title = ep.get("title", "")
        narrative = ep.get("narrative", "")
        topics = ", ".join(ep.get("topics", []))
        episode_lines.append(f"### {ep.get('week_start', '?')}\n{title}\n{narrative}\nTopics: {topics}")

    episodes_text = "\n\n".join(episode_lines)
    return f"""Synthesize these weekly episodes into a monthly summary for {month_name} {year}.

WEEKLY EPISODES:
{episodes_text}

Respond in EXACTLY this JSON format (no other text):
{{
  "summary": "3-5 sentence synthesis of the month — major themes, progress made, challenges overcome",
  "highlights": ["highlight1", "highlight2", "highlight3"]
}}

RULES:
- summary: synthesizes weekly narratives into a cohesive monthly story
- highlights: 3-5 most important themes/achievements/decisions of the month
"""


def _build_yearly_synthesis_prompt(
    monthly_summaries: List[Dict[str, Any]], year: int
) -> str:
    """Build LLM prompt for yearly summary from monthly summaries."""
    month_names = [
        "", "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    ]

    month_lines = []
    for ms in monthly_summaries:
        m = ms.get("month", 0)
        name = month_names[m] if 1 <= m <= 12 else str(m)
        summary = ms.get("summary", "")
        highlights = ", ".join(ms.get("highlights", []))
        month_lines.append(f"### {name}\n{summary}\nHighlights: {highlights}")

    months_text = "\n\n".join(month_lines)
    return f"""Synthesize these monthly summaries into a yearly summary for {year}.

MONTHLY SUMMARIES:
{months_text}

Respond in EXACTLY this JSON format (no other text):
{{
  "summary": "5-8 sentence synthesis of the year — major arcs, growth, milestones",
  "highlights": ["highlight1", "highlight2", "highlight3", "highlight4", "highlight5"]
}}

RULES:
- summary: captures the major narrative arcs of the year
- highlights: 5-10 most important milestones/themes/achievements
"""


# -- JSON response parsing (shared with DreamProcessor) ----------------------


def _parse_llm_json(raw: str | None) -> Optional[Dict[str, Any]]:
    """Parse LLM JSON response, handling common formatting issues."""
    if not raw:
        return None

    import re

    # Direct parse
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Markdown code block
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # First { to last }
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            pass

    logger.warning("Failed to parse LLM JSON: %s...", raw[:200])
    return None


# -- Consolidation Engine ----------------------------------------------------


class ConsolidationEngine:
    """Aggregates per-session dream data into weekly/monthly/yearly summaries
    and detects cross-session patterns.

    Args:
        state_db_path: Path to Hermes state.db
        memory_db_path: Path to memory.db (episodic memory)
        dream_data_dir: Base directory with per-session dream.json files
        output_dir: Directory for consolidation JSON outputs
        llm_caller: Optional callable ``[[str], str]`` for LLM narration.
            If None, uses deterministic fallback (concatenation).
        pattern_window: Number of recent sessions for pattern detection
    """

    def __init__(
        self,
        state_db_path: str | Path,
        memory_db_path: str | Path,
        dream_data_dir: str | Path,
        output_dir: str | Path,
        llm_caller: Optional[Callable[[str], str]] = None,
        pattern_window: int = DEFAULT_PATTERN_WINDOW,
    ):
        self._state_db = Path(state_db_path).expanduser()
        self._memory_db = Path(memory_db_path).expanduser()
        self._dream_data_dir = Path(dream_data_dir).expanduser()
        self._output_dir = Path(output_dir).expanduser()
        self._llm_caller = llm_caller
        self._pattern_window = pattern_window

    # -- Public API ----------------------------------------------------------

    def run(self, force: bool = False) -> Dict[str, Any]:
        """Run the full consolidation pipeline.

        Steps:
        1. Load all dream.json files from dream_data_dir
        2. Group by ISO week, consolidate unprocessed weeks
        3. Check month/year boundaries, generate summaries
        4. Detect cross-session patterns
        5. Push deduplicated facts to semantic memory

        Returns:
            Dict with consolidation metrics.
        """
        result = {
            "weekly_consolidated": 0,
            "monthly_consolidated": 0,
            "yearly_consolidated": 0,
            "patterns_detected": 0,
            "facts_pushed": 0,
            "errors": [],
        }

        # 1. Load all session dream data
        all_dreams = self._load_all_dreams()
        if not all_dreams:
            logger.info("No dream data found in %s", self._dream_data_dir)
            return result

        logger.info("Loaded %d session dreams", len(all_dreams))

        # 2. Group by ISO week and consolidate
        weeks = self._group_by_week(all_dreams)
        weekly_result = self._consolidate_weekly(weeks, force=force)
        result["weekly_consolidated"] = weekly_result["consolidated"]
        result["errors"].extend(weekly_result.get("errors", []))

        # 3. Monthly and yearly consolidation
        monthly_result = self._consolidate_monthly(force=force)
        result["monthly_consolidated"] = monthly_result["consolidated"]
        result["errors"].extend(monthly_result.get("errors", []))

        yearly_result = self._consolidate_yearly(force=force)
        result["yearly_consolidated"] = yearly_result["consolidated"]
        result["errors"].extend(yearly_result.get("errors", []))

        # 4. Pattern detection
        patterns = self._detect_patterns()
        result["patterns_detected"] = len(patterns)
        if patterns:
            self._save_patterns(patterns)

        # 5. Fact push
        facts_result = self._push_facts(all_dreams)
        result["facts_pushed"] = facts_result["pushed"]
        result["errors"].extend(facts_result.get("errors", []))

        logger.info(
            "Consolidation complete: %d weekly, %d monthly, %d yearly, "
            "%d patterns, %d facts pushed",
            result["weekly_consolidated"],
            result["monthly_consolidated"],
            result["yearly_consolidated"],
            result["patterns_detected"],
            result["facts_pushed"],
        )
        return result

    # -- Weekly Consolidation ------------------------------------------------

    def _load_all_dreams(self) -> List[Dict[str, Any]]:
        """Load all dream.json files from the dream data directory."""
        dreams = []
        if not self._dream_data_dir.exists():
            return dreams

        for session_dir in self._dream_data_dir.iterdir():
            if not session_dir.is_dir():
                continue
            dream_path = session_dir / "dream.json"
            if dream_path.exists():
                try:
                    data = json.loads(dream_path.read_text())
                    dreams.append(data)
                except Exception as e:
                    logger.warning("Failed to load dream %s: %s", dream_path, e)

        return sorted(dreams, key=lambda d: d.get("date", ""))

    def _group_by_week(
        self, dreams: List[Dict[str, Any]]
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Group session dreams by ISO week (YYYY-Www format)."""
        weeks: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for dream in dreams:
            date_str = dream.get("date", "")
            if not date_str:
                continue
            try:
                dt = datetime.fromisoformat(date_str)
                iso = dt.isocalendar()
                week_key = f"{iso[0]}-W{iso[1]:02d}"
                weeks[week_key].append(dream)
            except (ValueError, TypeError):
                logger.warning("Cannot parse date for session %s", dream.get("session_id"))
        return dict(weeks)

    def _consolidate_weekly(
        self, weeks: Dict[str, List[Dict[str, Any]]], force: bool = False
    ) -> Dict[str, Any]:
        """Consolidate each week that hasn't been processed yet."""
        result = {"consolidated": 0, "errors": []}

        for week_key, sessions in sorted(weeks.items()):
            try:
                # Check if already consolidated (JSON file exists)
                output_path = self._output_dir / f"weekly_{week_key}.json"
                if output_path.exists() and not force:
                    logger.debug("Week %s already consolidated, skipping", week_key)
                    continue

                # Compute week start/end from ISO week
                year, week_num = int(week_key[:4]), int(week_key[6:])
                week_start = date.fromisocalendar(year, week_num, 1)
                week_end = week_start + timedelta(days=6)

                # Generate narrative
                if self._llm_caller is not None:
                    narrative = self._llm_weekly_narrative(
                        sessions, week_start.isoformat(), week_end.isoformat()
                    )
                else:
                    narrative = self._heuristic_weekly_narrative(
                        sessions, week_start.isoformat(), week_end.isoformat()
                    )

                # Compute aggregates
                aggregates = self._compute_weekly_aggregates(sessions)

                # Build output
                weekly_data = {
                    "week_key": week_key,
                    "week_start": week_start.isoformat(),
                    "week_end": week_end.isoformat(),
                    "title": narrative.get("title", f"Week {week_key}"),
                    "narrative": narrative.get("narrative", ""),
                    "topics": narrative.get("topics", []),
                    "key_decisions": narrative.get("key_decisions", []),
                    "mood_arc": narrative.get("mood_arc", ""),
                    "aggregates": aggregates,
                    "session_ids": [s.get("session_id") for s in sessions],
                    "session_count": len(sessions),
                    "consolidated_at": int(datetime.now().timestamp()),
                }

                # Persist to JSON
                self._output_dir.mkdir(parents=True, exist_ok=True)
                output_path.write_text(json.dumps(weekly_data, indent=2, default=str))

                # Also persist to memory.db via EpisodicMemoryManager
                self._persist_weekly_to_db(weekly_data, sessions)

                result["consolidated"] += 1
                logger.info(
                    "Consolidated week %s: %d sessions, title=%s",
                    week_key, len(sessions), weekly_data["title"],
                )

            except Exception as e:
                logger.error("Failed to consolidate week %s: %s", week_key, e)
                result["errors"].append({"week": week_key, "error": str(e)})

        return result

    def _llm_weekly_narrative(
        self, sessions: List[Dict[str, Any]], week_start: str, week_end: str
    ) -> Dict[str, Any]:
        """Generate weekly narrative via LLM."""
        prompt = _build_weekly_narrative_prompt(sessions, week_start, week_end)
        assert self._llm_caller is not None  # caller guards this
        try:
            raw = self._llm_caller(prompt)
            parsed = _parse_llm_json(raw)
            if parsed and "title" in parsed and "narrative" in parsed:
                return parsed
            logger.warning("LLM returned incomplete weekly narrative, using fallback")
        except Exception as e:
            logger.warning("LLM call failed for weekly narrative: %s", e)

        return self._heuristic_weekly_narrative(sessions, week_start, week_end)

    @staticmethod
    def _heuristic_weekly_narrative(
        sessions: List[Dict[str, Any]], week_start: str, week_end: str
    ) -> Dict[str, Any]:
        """Deterministic fallback for weekly narrative."""
        titles = [s.get("title", "Untitled") for s in sessions]
        count = len(sessions)

        if count == 1:
            title = f"Week recap: {titles[0][:50]}"
        else:
            title = f"Week recap: {count} sessions"

        narrative_parts = [f"This week had {count} session(s)."]
        for s in sessions:
            summary = s.get("summary", "")
            if summary:
                narrative_parts.append(summary)
        narrative = " ".join(narrative_parts)

        # Collect topics
        all_tags = []
        for s in sessions:
            all_tags.extend(s.get("tags", []))
        topic_counts = Counter(all_tags)
        topics = [t for t, _ in topic_counts.most_common(7)]

        return {
            "title": title[:80],
            "narrative": narrative,
            "topics": topics or ["general"],
            "key_decisions": [],
            "mood_arc": "",
        }

    @staticmethod
    def _compute_weekly_aggregates(sessions: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Compute numeric aggregates from session dreams."""
        if not sessions:
            return {
                "dominant_tags": [],
                "avg_mood": "routine",
                "avg_importance": 0.0,
                "session_count": 0,
            }

        # Tag frequency
        all_tags = []
        for s in sessions:
            all_tags.extend(s.get("tags", []))
        tag_counts = Counter(all_tags)
        dominant_tags = [t for t, _ in tag_counts.most_common(5)]

        # Mood frequency
        moods = [s.get("mood", "routine") for s in sessions]
        mood_counts = Counter(moods)
        avg_mood = mood_counts.most_common(1)[0][0] if mood_counts else "routine"

        # Average importance
        importances = [s.get("importance", 3) for s in sessions]
        avg_importance = round(sum(importances) / len(importances), 1) if importances else 0.0

        return {
            "dominant_tags": dominant_tags,
            "avg_mood": avg_mood,
            "avg_importance": avg_importance,
            "session_count": len(sessions),
        }

    def _persist_weekly_to_db(
        self, weekly_data: Dict[str, Any], sessions: List[Dict[str, Any]]
    ) -> None:
        """Persist weekly consolidation to memory.db via EpisodicMemoryManager."""
        try:
            from agent.episodic_memory_manager import EpisodicMemoryManager

            em = EpisodicMemoryManager(self._memory_db)
            em.initialize()

            episode_id = em.create_episode(
                week_start=weekly_data["week_start"],
                week_end=weekly_data["week_end"],
                title=weekly_data["title"],
                narrative=weekly_data["narrative"],
                topics=weekly_data.get("topics", []),
                key_decisions=weekly_data.get("key_decisions", []),
                mood_arc=[{"mood_arc": weekly_data.get("mood_arc", "")}]
                if weekly_data.get("mood_arc")
                else [],
            )

            # Link sessions
            for sid in weekly_data.get("session_ids", []):
                em.link_session(episode_id, sid)

            logger.debug("Persisted weekly episode %d to memory.db", episode_id)

        except Exception as e:
            logger.warning("Failed to persist weekly to DB: %s", e)

    # -- Monthly Consolidation -----------------------------------------------

    def _consolidate_monthly(self, force: bool = False) -> Dict[str, Any]:
        """Check for completed months and generate monthly summaries."""
        result = {"consolidated": 0, "errors": []}

        # Find all weeks that have been consolidated
        if not self._output_dir.exists():
            return result

        # Group weeks by year-month
        month_weeks: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for f in sorted(self._output_dir.glob("weekly_*.json")):
            try:
                data = json.loads(f.read_text())
                ws = data.get("week_start", "")
                if ws:
                    dt = date.fromisoformat(ws)
                    month_key = f"{dt.year}-{dt.month:02d}"
                    month_weeks[month_key].append(data)
            except Exception:
                continue

        for month_key, weeks in sorted(month_weeks.items()):
            try:
                output_path = self._output_dir / f"monthly_{month_key}.json"
                if output_path.exists() and not force:
                    continue

                year, month = int(month_key[:4]), int(month_key[5:])

                # Check if month is complete (all weeks have ended)
                today = date.today()
                month_end = date(year, month, 28) + timedelta(days=4)
                month_end = month_end.replace(day=1) - timedelta(days=1)
                if month_end >= today and not force:
                    logger.debug("Month %s not yet complete, skipping", month_key)
                    continue

                # Generate summary
                if self._llm_caller is not None:
                    summary = self._llm_monthly_synthesis(weeks, year, month)
                else:
                    summary = self._heuristic_monthly_synthesis(weeks, year, month)

                monthly_data = {
                    "month_key": month_key,
                    "year": year,
                    "month": month,
                    "title": summary.get("title", f"Month {month_key}"),
                    "summary": summary.get("summary", ""),
                    "highlights": summary.get("highlights", []),
                    "week_count": len(weeks),
                    "total_sessions": sum(w.get("session_count", 0) for w in weeks),
                    "consolidated_at": int(datetime.now().timestamp()),
                }

                # Persist JSON
                self._output_dir.mkdir(parents=True, exist_ok=True)
                output_path.write_text(json.dumps(monthly_data, indent=2, default=str))

                # Persist to DB
                self._persist_monthly_to_db(monthly_data)

                result["consolidated"] += 1
                logger.info("Consolidated month %s: %d weeks", month_key, len(weeks))

            except Exception as e:
                logger.error("Failed to consolidate month %s: %s", month_key, e)
                result["errors"].append({"month": month_key, "error": str(e)})

        return result

    def _llm_monthly_synthesis(
        self, weeks: List[Dict[str, Any]], year: int, month: int
    ) -> Dict[str, Any]:
        """Generate monthly summary via LLM."""
        prompt = _build_monthly_synthesis_prompt(weeks, year, month)
        assert self._llm_caller is not None  # caller guards this
        try:
            raw = self._llm_caller(prompt)
            parsed = _parse_llm_json(raw)
            if parsed and "summary" in parsed:
                return parsed
            logger.warning("LLM returned incomplete monthly summary, using fallback")
        except Exception as e:
            logger.warning("LLM call failed for monthly synthesis: %s", e)

        return self._heuristic_monthly_synthesis(weeks, year, month)

    @staticmethod
    def _heuristic_monthly_synthesis(
        weeks: List[Dict[str, Any]], year: int, month: int
    ) -> Dict[str, Any]:
        """Deterministic fallback for monthly summary."""
        month_names = [
            "", "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        ]
        month_name = month_names[month] if 1 <= month <= 12 else str(month)

        narratives = [w.get("narrative", "") for w in weeks if w.get("narrative")]
        all_topics = []
        all_decisions = []
        for w in weeks:
            all_topics.extend(w.get("topics", []))
            all_decisions.extend(w.get("key_decisions", []))

        summary_parts = [f"{month_name} {year}: {len(weeks)} week(s) summarized."]
        summary_parts.extend(narratives)
        summary_text = "\n\n".join(summary_parts)

        # Highlights from top topics + decisions
        seen = set()
        highlights = []
        for item in all_topics + all_decisions:
            if item not in seen:
                seen.add(item)
                highlights.append(item)
            if len(highlights) >= 5:
                break

        return {
            "title": f"{month_name} {year} summary",
            "summary": summary_text,
            "highlights": highlights,
        }

    def _persist_monthly_to_db(self, monthly_data: Dict[str, Any]) -> None:
        """Persist monthly summary to memory.db."""
        try:
            from agent.episodic_memory import EpisodicMemory

            em = EpisodicMemory(self._memory_db)
            em.initialize()
            em.generate_monthly_summary(
                year=monthly_data["year"],
                month=monthly_data["month"],
                synthesizer=lambda episodes: (
                    monthly_data["summary"],
                    monthly_data.get("highlights", []),
                ),
            )
            logger.debug("Persisted monthly summary %s to DB", monthly_data["month_key"])
        except Exception as e:
            logger.warning("Failed to persist monthly to DB: %s", e)

    # -- Yearly Consolidation ------------------------------------------------

    def _consolidate_yearly(self, force: bool = False) -> Dict[str, Any]:
        """Check for completed years and generate yearly summaries."""
        result = {"consolidated": 0, "errors": []}

        if not self._output_dir.exists():
            return result

        # Group months by year
        year_months: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
        for f in sorted(self._output_dir.glob("monthly_*.json")):
            try:
                data = json.loads(f.read_text())
                year = data.get("year")
                if year:
                    year_months[year].append(data)
            except Exception:
                continue

        for year, months in sorted(year_months.items()):
            try:
                output_path = self._output_dir / f"yearly_{year}.json"
                if output_path.exists() and not force:
                    continue

                # Need at least 2 months for a yearly summary
                if len(months) < 2 and not force:
                    logger.debug("Only %d months for %d, need >=2", len(months), year)
                    continue

                # Check if year is complete
                today = date.today()
                if year >= today.year and not force:
                    logger.debug("Year %d not yet complete, skipping", year)
                    continue

                # Generate summary
                if self._llm_caller is not None:
                    summary = self._llm_yearly_synthesis(months, year)
                else:
                    summary = self._heuristic_yearly_synthesis(months, year)

                yearly_data = {
                    "year": year,
                    "title": summary.get("title", f"Year {year}"),
                    "summary": summary.get("summary", ""),
                    "highlights": summary.get("highlights", []),
                    "month_count": len(months),
                    "total_sessions": sum(m.get("total_sessions", 0) for m in months),
                    "consolidated_at": int(datetime.now().timestamp()),
                }

                # Persist JSON
                self._output_dir.mkdir(parents=True, exist_ok=True)
                output_path.write_text(json.dumps(yearly_data, indent=2, default=str))

                # Persist to DB
                self._persist_yearly_to_db(yearly_data)

                result["consolidated"] += 1
                logger.info("Consolidated year %d: %d months", year, len(months))

            except Exception as e:
                logger.error("Failed to consolidate year %d: %s", year, e)
                result["errors"].append({"year": year, "error": str(e)})

        return result

    def _llm_yearly_synthesis(
        self, months: List[Dict[str, Any]], year: int
    ) -> Dict[str, Any]:
        """Generate yearly summary via LLM."""
        prompt = _build_yearly_synthesis_prompt(months, year)
        assert self._llm_caller is not None  # caller guards this
        try:
            raw = self._llm_caller(prompt)
            parsed = _parse_llm_json(raw)
            if parsed and "summary" in parsed:
                return parsed
            logger.warning("LLM returned incomplete yearly summary, using fallback")
        except Exception as e:
            logger.warning("LLM call failed for yearly synthesis: %s", e)

        return self._heuristic_yearly_synthesis(months, year)

    @staticmethod
    def _heuristic_yearly_synthesis(
        months: List[Dict[str, Any]], year: int
    ) -> Dict[str, Any]:
        """Deterministic fallback for yearly summary."""
        month_names = [
            "", "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        ]

        summary_parts = [f"Year in review: {year}. {len(months)} month(s) summarized."]
        for m in months:
            mo = m.get("month", 0)
            name = month_names[mo] if 1 <= mo <= 12 else str(mo)
            summary_parts.append(f"\n--- {name} ---\n{m.get('summary', '')}")

        summary_text = "\n".join(summary_parts)

        # Aggregate highlights
        seen = set()
        highlights = []
        for m in months:
            for h in m.get("highlights", []):
                if h not in seen:
                    seen.add(h)
                    highlights.append(h)
                if len(highlights) >= 10:
                    break
            if len(highlights) >= 10:
                break

        return {
            "title": f"Year {year} in review",
            "summary": summary_text,
            "highlights": highlights,
        }

    def _persist_yearly_to_db(self, yearly_data: Dict[str, Any]) -> None:
        """Persist yearly summary to memory.db."""
        try:
            from agent.episodic_memory import EpisodicMemory

            em = EpisodicMemory(self._memory_db)
            em.initialize()
            em.generate_yearly_summary(
                year=yearly_data["year"],
                synthesizer=lambda summaries: (
                    yearly_data["summary"],
                    yearly_data.get("highlights", []),
                ),
            )
            logger.debug("Persisted yearly summary %d to DB", yearly_data["year"])
        except Exception as e:
            logger.warning("Failed to persist yearly to DB: %s", e)

    # -- Pattern Detection ---------------------------------------------------

    def _detect_patterns(self) -> List[Dict[str, Any]]:
        """Analyze recent sessions for cross-session patterns.

        Looks for:
        - Recurring themes (tags that appear in >=3 sessions)
        - Evolving topics (tags that appear more frequently over time)
        - Long-term mood trends
        - Repeated entity/concern mentions
        """
        # Load recent session dreams
        all_dreams = self._load_all_dreams()
        recent = all_dreams[-self._pattern_window:] if len(all_dreams) > self._pattern_window else all_dreams

        if len(recent) < 3:
            logger.info("Not enough sessions (%d) for pattern detection", len(recent))
            return []

        patterns: List[Dict[str, Any]] = []

        # 1. Recurring themes
        patterns.extend(self._detect_recurring_themes(recent))

        # 2. Evolving topics
        patterns.extend(self._detect_evolving_topics(recent))

        # 3. Mood trends
        patterns.extend(self._detect_mood_trends(recent))

        # 4. Importance trends
        patterns.extend(self._detect_importance_trends(recent))

        return patterns

    @staticmethod
    def _detect_recurring_themes(
        dreams: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Find tags that appear in >= 3 sessions."""
        patterns = []

        # Count tag occurrences across sessions
        tag_sessions: Dict[str, List[str]] = defaultdict(list)
        for d in dreams:
            sid = d.get("session_id", "?")
            for tag in d.get("tags", []):
                tag_sessions[tag].append(sid)

        for tag, sessions in tag_sessions.items():
            if len(sessions) >= 3:
                patterns.append({
                    "pattern_type": "recurring_theme",
                    "description": f"Theme '{tag}' appears in {len(sessions)} sessions",
                    "evidence_sessions": sessions,
                    "confidence": min(1.0, len(sessions) / len(dreams)),
                })

        return patterns

    @staticmethod
    def _detect_evolving_topics(
        dreams: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Find tags that appear more frequently in recent sessions vs older."""
        patterns = []
        if len(dreams) < 6:
            return patterns

        mid = len(dreams) // 2
        older = dreams[:mid]
        newer = dreams[mid:]

        older_tags: Counter = Counter()
        newer_tags: Counter = Counter()
        for d in older:
            older_tags.update(d.get("tags", []))
        for d in newer:
            newer_tags.update(d.get("tags", []))

        for tag in newer_tags:
            old_count = older_tags.get(tag, 0)
            new_count = newer_tags[tag]
            if new_count >= 3 and new_count > old_count * 1.5:
                patterns.append({
                    "pattern_type": "evolving_topic",
                    "description": f"Topic '{tag}' is trending up ({old_count} → {new_count})",
                    "evidence_sessions": [
                        d.get("session_id", "?") for d in newer if tag in d.get("tags", [])
                    ],
                    "confidence": min(1.0, new_count / (old_count + 1) / 2),
                })

        return patterns

    @staticmethod
    def _detect_mood_trends(
        dreams: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Detect long-term mood trends."""
        patterns = []
        if len(dreams) < 5:
            return patterns

        # Simple trend: count positive vs negative moods in first/second half
        positive_moods = {"exploratory", "focused", "celebratory", "creative", "reflective"}
        negative_moods = {"frustrated", "urgent"}

        mid = len(dreams) // 2
        older = dreams[:mid]
        newer = dreams[mid:]

        def mood_score(ds: List[Dict[str, Any]]) -> float:
            if not ds:
                return 0.0
            pos = sum(1 for d in ds if d.get("mood") in positive_moods)
            neg = sum(1 for d in ds if d.get("mood") in negative_moods)
            return (pos - neg) / len(ds)

        old_score = mood_score(older)
        new_score = mood_score(newer)
        delta = new_score - old_score

        if abs(delta) >= 0.2:
            direction = "improving" if delta > 0 else "declining"
            patterns.append({
                "pattern_type": "mood_trend",
                "description": f"Overall mood is {direction} (score: {old_score:.2f} → {new_score:.2f})",
                "evidence_sessions": [d.get("session_id", "?") for d in dreams],
                "confidence": min(1.0, abs(delta) * 2),
            })

        return patterns

    @staticmethod
    def _detect_importance_trends(
        dreams: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Detect shifts in average importance."""
        patterns = []
        if len(dreams) < 6:
            return patterns

        mid = len(dreams) // 2
        older = dreams[:mid]
        newer = dreams[mid:]

        old_avg = sum(d.get("importance", 3) for d in older) / len(older) if older else 0
        new_avg = sum(d.get("importance", 3) for d in newer) / len(newer) if newer else 0
        delta = new_avg - old_avg

        if abs(delta) >= 0.5:
            direction = "increasing" if delta > 0 else "decreasing"
            patterns.append({
                "pattern_type": "importance_trend",
                "description": f"Session importance is {direction} (avg: {old_avg:.1f} → {new_avg:.1f})",
                "evidence_sessions": [d.get("session_id", "?") for d in dreams],
                "confidence": min(1.0, abs(delta) / 2),
            })

        return patterns

    def _save_patterns(self, patterns: List[Dict[str, Any]]) -> None:
        """Save detected patterns to dream_patterns.json."""
        self._output_dir.mkdir(parents=True, exist_ok=True)
        output_path = self._output_dir / "dream_patterns.json"

        # Merge with existing patterns
        existing = []
        if output_path.exists():
            try:
                existing = json.loads(output_path.read_text())
            except Exception:
                existing = []

        # Deduplicate by (pattern_type, description)
        seen = {(p.get("pattern_type"), p.get("description")) for p in existing}
        for p in patterns:
            key = (p.get("pattern_type"), p.get("description"))
            if key not in seen:
                existing.append(p)
                seen.add(key)

        output_path.write_text(json.dumps(existing, indent=2, default=str))
        logger.info("Saved %d patterns to %s", len(existing), output_path)

    # -- Fact Push -----------------------------------------------------------

    def _push_facts(self, dreams: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Push extracted facts to semantic memory with deduplication.

        Reads facts from each session's dream.json, deduplicates against
        existing semantic memory entries, and pushes new unique facts.
        """
        result = {"pushed": 0, "errors": []}

        # Load existing facts from prior consolidations
        facts_file = self._output_dir / "pushed_facts.json"
        existing_facts: set = set()
        if facts_file.exists():
            try:
                data = json.loads(facts_file.read_text())
                existing_facts = {f.get("content", "") for f in data}
            except Exception:
                existing_facts = set()

        new_facts = []
        for dream in dreams:
            for fact in dream.get("facts", []):
                content = fact.get("content", "").strip()
                if not content or content in existing_facts:
                    continue

                # Normalize for dedup (lowercase, stripped)
                normalized = content.lower().strip()
                if normalized in existing_facts:
                    continue

                fact_entry = {
                    "fact_type": fact.get("fact_type", "insight"),
                    "content": content,
                    "confidence": fact.get("confidence", 0.5),
                    "source_session": dream.get("session_id", ""),
                    "pushed_at": int(datetime.now().timestamp()),
                }
                new_facts.append(fact_entry)
                existing_facts.add(content)
                existing_facts.add(normalized)

        if new_facts:
            # Append to pushed_facts.json
            all_pushed = []
            if facts_file.exists():
                try:
                    all_pushed = json.loads(facts_file.read_text())
                except Exception:
                    all_pushed = []
            all_pushed.extend(new_facts)

            self._output_dir.mkdir(parents=True, exist_ok=True)
            facts_file.write_text(json.dumps(all_pushed, indent=2, default=str))
            result["pushed"] = len(new_facts)
            logger.info("Pushed %d new facts to %s", len(new_facts), facts_file)

        return result
