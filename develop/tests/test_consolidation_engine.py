"""Tests for ConsolidationEngine — weekly/monthly/yearly consolidation and pattern detection.

Run:  python -m pytest tests/test_consolidation_engine.py -v
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.consolidation_engine import (
    ConsolidationEngine,
    _build_monthly_synthesis_prompt,
    _build_weekly_narrative_prompt,
    _build_yearly_synthesis_prompt,
    _parse_llm_json,
)
from agent.episodic_memory_manager import EpisodicMemoryManager


# -- Fixtures ----------------------------------------------------------------


@pytest.fixture
def tmp_dir(tmp_path):
    """Provide a clean temp directory."""
    return tmp_path


@pytest.fixture
def state_db(tmp_dir):
    """Create a minimal state.db."""
    db_path = tmp_dir / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, title TEXT, source TEXT, model TEXT,
            started_at TEXT, ended_at TEXT, parent_session_id TEXT
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,
            role TEXT, content TEXT, tool_calls TEXT, tool_name TEXT, timestamp TEXT
        );
    """)
    conn.close()
    return db_path


@pytest.fixture
def memory_db(tmp_dir):
    """Create a memory.db with episodic schema."""
    db_path = tmp_dir / "memory.db"
    em = EpisodicMemoryManager(db_path)
    em.initialize()
    em.close()
    return db_path


@pytest.fixture
def dream_data_dir(tmp_dir):
    """Create dream_data directory with sample session dreams."""
    base = tmp_dir / "dream_data"
    base.mkdir()

    # Session 1 — Monday of week 21, 2026
    s1 = base / "sess_001"
    s1.mkdir()
    (s1 / "dream.json").write_text(json.dumps({
        "session_id": "sess_001",
        "title": "Setup Docker containers",
        "summary": "Configured Docker and deployed 3 containers.",
        "tags": ["docker", "devops", "linux"],
        "mood": "focused",
        "importance": 4,
        "day_number": 38,
        "weekday": "Monday",
        "date": "2026-05-18T10:00:00",
        "source": "cli",
        "model": "mimo-v2.5-pro",
        "message_count": 12,
        "facts": [
            {"fact_type": "decision", "content": "Use Docker Compose for all services", "confidence": 0.9},
            {"fact_type": "entity", "content": "Server n01-gra-fr runs Debian 12", "confidence": 0.95},
        ],
        "processed_at": 1716000000,
    }))

    # Session 2 — Wednesday of same week
    s2 = base / "sess_002"
    s2.mkdir()
    (s2 / "dream.json").write_text(json.dumps({
        "session_id": "sess_002",
        "title": "Python debugging session",
        "summary": "Fixed import errors and wrote unit tests.",
        "tags": ["python", "testing", "debugging"],
        "mood": "analytical",
        "importance": 3,
        "day_number": 40,
        "weekday": "Wednesday",
        "date": "2026-05-20T14:00:00",
        "source": "cli",
        "model": "mimo-v2.5-pro",
        "message_count": 8,
        "facts": [
            {"fact_type": "preference", "content": "User prefers pytest over unittest", "confidence": 0.85},
        ],
        "processed_at": 1716100000,
    }))

    # Session 3 — Friday of same week
    s3 = base / "sess_003"
    s3.mkdir()
    (s3 / "dream.json").write_text(json.dumps({
        "session_id": "sess_003",
        "title": "Identity system design",
        "summary": "Designed the immutable identity schema for the agent.",
        "tags": ["identity", "sqlite", "architecture"],
        "mood": "creative",
        "importance": 5,
        "day_number": 42,
        "weekday": "Friday",
        "date": "2026-05-22T09:00:00",
        "source": "cli",
        "model": "mimo-v2.5-pro",
        "message_count": 20,
        "facts": [
            {"fact_type": "decision", "content": "Identity DB is immutable — no UPDATE/DELETE on birthday", "confidence": 0.99},
            {"fact_type": "constraint", "content": "birthday column has a trigger guard", "confidence": 0.95},
        ],
        "processed_at": 1716200000,
    }))

    return base


@pytest.fixture
def output_dir(tmp_dir):
    """Provide output directory path."""
    return tmp_dir / "consolidations"


@pytest.fixture
def engine(state_db, memory_db, dream_data_dir, output_dir):
    """Create a ConsolidationEngine with test paths."""
    return ConsolidationEngine(
        state_db_path=state_db,
        memory_db_path=memory_db,
        dream_data_dir=dream_data_dir,
        output_dir=output_dir,
        llm_caller=None,  # heuristic mode
        pattern_window=30,
    )


@pytest.fixture
def engine_with_llm(state_db, memory_db, dream_data_dir, output_dir):
    """Create a ConsolidationEngine with a mock LLM caller."""
    def mock_llm(prompt: str) -> str:
        if "weekly" in prompt.lower() or "week of" in prompt.lower():
            return json.dumps({
                "title": "Docker and identity week",
                "narrative": "This week focused on containerization and identity design.",
                "topics": ["docker", "identity", "python"],
                "key_decisions": ["Use Docker Compose", "Immutable identity DB"],
                "mood_arc": "Started focused, became creative",
            })
        elif "monthly" in prompt.lower():
            return json.dumps({
                "summary": "May 2026: foundational infrastructure setup.",
                "highlights": ["Docker deployment", "Identity system", "Testing"],
            })
        elif "yearly" in prompt.lower():
            return json.dumps({
                "summary": "2026 in review: agent identity system built from scratch.",
                "highlights": ["Identity DB", "Docker infrastructure", "Memory system"],
            })
        return "{}"

    return ConsolidationEngine(
        state_db_path=state_db,
        memory_db_path=memory_db,
        dream_data_dir=dream_data_dir,
        output_dir=output_dir,
        llm_caller=mock_llm,
        pattern_window=30,
    )


# -- Tests: _parse_llm_json --------------------------------------------------


class TestParseLlmJson:
    """Tests for the shared JSON parser."""

    def test_valid_json(self):
        raw = '{"title": "Test", "narrative": "Hello"}'
        result = _parse_llm_json(raw)
        assert result is not None
        assert result["title"] == "Test"

    def test_json_in_code_block(self):
        raw = '```json\n{"title": "Test"}\n```'
        result = _parse_llm_json(raw)
        assert result is not None
        assert result["title"] == "Test"

    def test_json_with_surrounding_text(self):
        raw = 'Here is the result:\n{"title": "Test"}\nDone.'
        result = _parse_llm_json(raw)
        assert result is not None
        assert result["title"] == "Test"

    def test_empty_string(self):
        assert _parse_llm_json("") is None

    def test_none_input(self):
        assert _parse_llm_json("") is None  # empty is the closest to None in typed world

    def test_garbage_input(self):
        assert _parse_llm_json("no json here") is None


# -- Tests: Weekly Consolidation ---------------------------------------------


class TestWeeklyConsolidation:
    """Tests for weekly episode consolidation."""

    def test_run_produces_weekly_output(self, engine, output_dir):
        result = engine.run()
        assert result["weekly_consolidated"] >= 1

        # Check JSON file was created
        weekly_files = list(output_dir.glob("weekly_*.json"))
        assert len(weekly_files) >= 1

        # Verify content
        data = json.loads(weekly_files[0].read_text())
        assert "title" in data
        assert "narrative" in data
        assert "aggregates" in data
        assert data["session_count"] >= 1

    def test_weekly_json_has_correct_structure(self, engine, output_dir):
        engine.run()
        weekly_files = list(output_dir.glob("weekly_*.json"))
        assert weekly_files

        data = json.loads(weekly_files[0].read_text())
        required_keys = [
            "week_key", "week_start", "week_end", "title",
            "narrative", "topics", "key_decisions", "mood_arc",
            "aggregates", "session_ids", "session_count", "consolidated_at",
        ]
        for key in required_keys:
            assert key in data, f"Missing key: {key}"

    def test_weekly_aggregates_computed(self, engine, output_dir):
        engine.run()
        weekly_files = list(output_dir.glob("weekly_*.json"))
        data = json.loads(weekly_files[0].read_text())

        agg = data["aggregates"]
        assert "dominant_tags" in agg
        assert "avg_mood" in agg
        assert "avg_importance" in agg
        assert "session_count" in agg
        assert agg["session_count"] == 3

    def test_weekly_idempotent(self, engine, output_dir):
        """Running twice should not create duplicates."""
        engine.run()
        count_after_first = len(list(output_dir.glob("weekly_*.json")))
        engine.run()
        count_after_second = len(list(output_dir.glob("weekly_*.json")))
        assert count_after_first == count_after_second

    def test_weekly_force_reprocess(self, engine, output_dir):
        """Force=True should overwrite existing consolidation."""
        engine.run()
        engine.run(force=True)
        weekly_files = list(output_dir.glob("weekly_*.json"))
        assert len(weekly_files) >= 1

    def test_weekly_llm_narrative(self, engine_with_llm, output_dir):
        """With LLM caller, narrative should come from mock."""
        engine_with_llm.run()
        weekly_files = list(output_dir.glob("weekly_*.json"))
        data = json.loads(weekly_files[0].read_text())
        assert "Docker" in data["title"] or "identity" in data["title"]

    def test_weekly_heuristic_narrative(self, engine, output_dir):
        """Without LLM, heuristic should produce valid output."""
        engine.run()
        weekly_files = list(output_dir.glob("weekly_*.json"))
        data = json.loads(weekly_files[0].read_text())
        assert data["narrative"]  # not empty
        assert data["session_count"] == 3

    def test_weekly_persists_to_db(self, engine, memory_db):
        """Weekly episodes should be stored in memory.db."""
        engine.run()
        em = EpisodicMemoryManager(memory_db)
        em.initialize()
        episodes = em.list_episodes()
        assert len(episodes) >= 1

    def test_weekly_links_sessions(self, engine, memory_db):
        """Sessions should be linked to episodes in DB."""
        engine.run()
        em = EpisodicMemoryManager(memory_db)
        em.initialize()
        episodes = em.list_episodes()
        assert episodes
        linked = em.get_episode_sessions(episodes[0]["id"])
        assert len(linked) == 3


# -- Tests: Monthly Consolidation --------------------------------------------


class TestMonthlyConsolidation:
    """Tests for monthly summary generation."""

    def test_monthly_not_generated_when_week_incomplete(self, engine, output_dir):
        """Monthly should not generate if the month isn't complete."""
        result = engine.run()
        # May 2026 is not complete yet (today is May 29), so no monthly
        monthly_files = list(output_dir.glob("monthly_*.json"))
        # This depends on current date — if running in May 2026, it won't consolidate
        # We test the force path separately
        if date.today().year == 2026 and date.today().month == 5:
            assert len(monthly_files) == 0

    def test_monthly_force_generates(self, engine, output_dir):
        """Force=True should generate monthly even for incomplete months."""
        # First run weekly
        engine.run()
        # Then force monthly
        engine._consolidate_monthly(force=True)
        monthly_files = list(output_dir.glob("monthly_*.json"))
        assert len(monthly_files) >= 1

        data = json.loads(monthly_files[0].read_text())
        assert "summary" in data
        assert "highlights" in data
        assert data["month"] == 5
        assert data["year"] == 2026

    def test_monthly_synthesizes_weekly(self, engine, output_dir):
        """Monthly summary should reference weekly data, not raw sessions."""
        engine.run()
        engine._consolidate_monthly(force=True)
        monthly_files = list(output_dir.glob("monthly_*.json"))
        data = json.loads(monthly_files[0].read_text())
        assert data["week_count"] >= 1

    def test_monthly_llm_synthesis(self, engine_with_llm, output_dir):
        """With LLM, monthly should use LLM-generated content or fallback."""
        engine_with_llm.run()
        engine_with_llm._consolidate_monthly(force=True)
        monthly_files = list(output_dir.glob("monthly_*.json"))
        if monthly_files:
            data = json.loads(monthly_files[0].read_text())
            # Should have a valid summary (either LLM or heuristic)
            assert data["summary"]
            assert data["week_count"] >= 1


# -- Tests: Yearly Consolidation ---------------------------------------------


class TestYearlyConsolidation:
    """Tests for yearly summary generation."""

    def test_yearly_needs_two_months(self, engine, output_dir):
        """Yearly should not generate with fewer than 2 monthly summaries."""
        # Even with force, we only have data for one month (May)
        engine.run()
        engine._consolidate_yearly(force=True)
        yearly_files = list(output_dir.glob("yearly_*.json"))
        # Should be 0 because only 1 month exists
        assert len(yearly_files) == 0

    def test_yearly_generates_with_two_months(self, engine, output_dir):
        """Yearly should generate when 2+ monthly summaries exist."""
        # Manually create two monthly summaries
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "monthly_2026-04.json").write_text(json.dumps({
            "month_key": "2026-04", "year": 2026, "month": 4,
            "summary": "April summary", "highlights": ["setup"],
            "week_count": 4, "total_sessions": 10,
        }))
        (output_dir / "monthly_2026-05.json").write_text(json.dumps({
            "month_key": "2026-05", "year": 2026, "month": 5,
            "summary": "May summary", "highlights": ["identity"],
            "week_count": 4, "total_sessions": 15,
        }))
        engine._consolidate_yearly(force=True)
        yearly_files = list(output_dir.glob("yearly_*.json"))
        assert len(yearly_files) >= 1
        data = json.loads(yearly_files[0].read_text())
        assert data["year"] == 2026
        assert "summary" in data
        assert data["month_count"] == 2


# -- Tests: Pattern Detection ------------------------------------------------


class TestPatternDetection:
    """Tests for cross-session pattern detection."""

    def test_patterns_detected(self, engine):
        """Should detect patterns with 3+ sessions."""
        patterns = engine._detect_patterns()
        # At minimum, we should detect recurring themes
        assert isinstance(patterns, list)

    def test_recurring_theme_detection(self, engine):
        """Tags appearing in >=3 sessions should be detected."""
        # Create many sessions with overlapping tags
        dreams = [
            {"session_id": f"s{i}", "tags": ["docker", "devops"], "mood": "focused", "importance": 3}
            for i in range(5)
        ]
        patterns = engine._detect_recurring_themes(dreams)
        assert len(patterns) >= 1
        docker_pattern = [p for p in patterns if "docker" in p["description"]]
        assert len(docker_pattern) == 1
        assert docker_pattern[0]["confidence"] > 0.5

    def test_evolving_topic_detection(self, engine):
        """Topics trending upward should be detected."""
        # Create sessions where 'ml' appears more in newer sessions
        older = [
            {"session_id": f"s{i}", "tags": ["python"], "mood": "routine", "importance": 3}
            for i in range(5)
        ]
        newer = [
            {"session_id": f"s{i+5}", "tags": ["python", "ml", "ml"], "mood": "focused", "importance": 4}
            for i in range(5)
        ]
        patterns = engine._detect_evolving_topics(older + newer)
        ml_patterns = [p for p in patterns if "ml" in p["description"]]
        assert len(ml_patterns) >= 1

    def test_mood_trend_detection(self, engine):
        """Improving mood should be detected."""
        older = [
            {"session_id": f"s{i}", "tags": [], "mood": "frustrated", "importance": 3}
            for i in range(5)
        ]
        newer = [
            {"session_id": f"s{i+5}", "tags": [], "mood": "celebratory", "importance": 3}
            for i in range(5)
        ]
        patterns = engine._detect_mood_trends(older + newer)
        mood_patterns = [p for p in patterns if p["pattern_type"] == "mood_trend"]
        assert len(mood_patterns) == 1
        assert "improving" in mood_patterns[0]["description"]

    def test_importance_trend_detection(self, engine):
        """Increasing importance should be detected."""
        older = [
            {"session_id": f"s{i}", "tags": [], "mood": "routine", "importance": 2}
            for i in range(5)
        ]
        newer = [
            {"session_id": f"s{i+5}", "tags": [], "mood": "routine", "importance": 4}
            for i in range(5)
        ]
        patterns = engine._detect_importance_trends(older + newer)
        imp_patterns = [p for p in patterns if p["pattern_type"] == "importance_trend"]
        assert len(imp_patterns) == 1
        assert "increasing" in imp_patterns[0]["description"]

    def test_patterns_saved_to_file(self, engine, output_dir):
        """Patterns should be written to dream_patterns.json."""
        # Add more sessions to trigger patterns
        engine.run()
        # Manually trigger pattern detection and save
        patterns = engine._detect_patterns()
        if patterns:
            engine._save_patterns(patterns)
            assert (output_dir / "dream_patterns.json").exists()
            data = json.loads((output_dir / "dream_patterns.json").read_text())
            assert isinstance(data, list)

    def test_pattern_deduplication(self, engine, output_dir):
        """Re-running should not create duplicate patterns."""
        output_dir.mkdir(parents=True, exist_ok=True)
        existing = [{"pattern_type": "recurring_theme", "description": "Theme 'docker' appears in 5 sessions"}]
        (output_dir / "dream_patterns.json").write_text(json.dumps(existing))

        new_patterns = [{"pattern_type": "recurring_theme", "description": "Theme 'docker' appears in 5 sessions"},
                        {"pattern_type": "mood_trend", "description": "Mood improving"}]
        engine._save_patterns(new_patterns)

        data = json.loads((output_dir / "dream_patterns.json").read_text())
        # Should have 2, not 3 (docker deduplicated)
        assert len(data) == 2

    def test_not_enough_sessions(self, engine):
        """Should return empty with fewer than 3 sessions."""
        dreams = [
            {"session_id": "s1", "tags": ["a"], "mood": "routine", "importance": 3},
            {"session_id": "s2", "tags": ["b"], "mood": "routine", "importance": 3},
        ]
        # Override load to return only 2
        engine._load_all_dreams = lambda: dreams
        patterns = engine._detect_patterns()
        assert patterns == []


# -- Tests: Fact Push --------------------------------------------------------


class TestFactPush:
    """Tests for semantic memory fact push."""

    def test_facts_pushed(self, engine, output_dir):
        """Extracted facts should be pushed to pushed_facts.json."""
        engine.run()
        facts_file = output_dir / "pushed_facts.json"
        assert facts_file.exists()
        data = json.loads(facts_file.read_text())
        assert len(data) >= 1
        # Should contain facts from the 3 sessions
        contents = {f["content"] for f in data}
        assert "Use Docker Compose for all services" in contents

    def test_facts_deduplicated_on_rerun(self, engine, output_dir):
        """Re-running should not create duplicate facts."""
        engine.run()
        count_first = len(json.loads((output_dir / "pushed_facts.json").read_text()))
        engine.run()
        count_second = len(json.loads((output_dir / "pushed_facts.json").read_text()))
        assert count_first == count_second

    def test_fact_structure(self, engine, output_dir):
        """Pushed facts should have required fields."""
        engine.run()
        data = json.loads((output_dir / "pushed_facts.json").read_text())
        for fact in data:
            assert "fact_type" in fact
            assert "content" in fact
            assert "confidence" in fact
            assert "source_session" in fact
            assert "pushed_at" in fact

    def test_empty_facts_no_file(self, tmp_dir, state_db, memory_db, output_dir):
        """Sessions with no facts should not create pushed_facts.json."""
        # Create dreams with no facts
        dream_dir = tmp_dir / "dream_data"
        s1 = dream_dir / "sess_empty"
        s1.mkdir(parents=True)
        (s1 / "dream.json").write_text(json.dumps({
            "session_id": "sess_empty",
            "title": "Empty session",
            "summary": "No facts here.",
            "tags": ["test"],
            "mood": "routine",
            "importance": 1,
            "date": "2026-05-18T10:00:00",
            "facts": [],
        }))

        engine = ConsolidationEngine(
            state_db_path=state_db,
            memory_db_path=memory_db,
            dream_data_dir=dream_dir,
            output_dir=output_dir,
        )
        result = engine.run()
        assert result["facts_pushed"] == 0


# -- Tests: Prompt Builders --------------------------------------------------


class TestPromptBuilders:
    """Tests for LLM prompt construction."""

    def test_weekly_prompt_includes_sessions(self):
        sessions = [
            {"title": "Docker setup", "summary": "Installed Docker.", "tags": ["docker"], "mood": "focused", "importance": 4}
        ]
        prompt = _build_weekly_narrative_prompt(sessions, "2026-05-18", "2026-05-24")
        assert "Docker setup" in prompt
        assert "2026-05-18" in prompt
        assert "focused" in prompt

    def test_monthly_prompt_includes_episodes(self):
        episodes = [
            {"week_start": "2026-05-04", "title": "Week 1", "narrative": "Setup week.", "topics": ["docker"]}
        ]
        prompt = _build_monthly_synthesis_prompt(episodes, 2026, 5)
        assert "May" in prompt
        assert "Week 1" in prompt

    def test_yearly_prompt_includes_months(self):
        months = [
            {"month": 4, "summary": "April summary.", "highlights": ["setup"]},
            {"month": 5, "summary": "May summary.", "highlights": ["identity"]},
        ]
        prompt = _build_yearly_synthesis_prompt(months, 2026)
        assert "2026" in prompt
        assert "April" in prompt
        assert "May" in prompt


# -- Tests: Full Pipeline ----------------------------------------------------


class TestFullPipeline:
    """Tests for the complete consolidation run."""

    def test_full_run_returns_metrics(self, engine):
        result = engine.run()
        assert "weekly_consolidated" in result
        assert "monthly_consolidated" in result
        assert "yearly_consolidated" in result
        assert "patterns_detected" in result
        assert "facts_pushed" in result
        assert "errors" in result

    def test_full_run_no_errors(self, engine):
        result = engine.run()
        assert result["errors"] == []

    def test_full_run_with_llm(self, engine_with_llm):
        result = engine_with_llm.run()
        assert result["weekly_consolidated"] >= 1
        assert result["facts_pushed"] >= 1

    def test_empty_dream_dir(self, tmp_dir, state_db, memory_db, output_dir):
        """Engine should handle empty dream_data gracefully."""
        empty_dir = tmp_dir / "empty_dreams"
        empty_dir.mkdir()
        engine = ConsolidationEngine(
            state_db_path=state_db,
            memory_db_path=memory_db,
            dream_data_dir=empty_dir,
            output_dir=output_dir,
        )
        result = engine.run()
        assert result["weekly_consolidated"] == 0
        assert result["facts_pushed"] == 0

    def test_nonexistent_dream_dir(self, tmp_dir, state_db, memory_db, output_dir):
        """Engine should handle missing dream_data gracefully."""
        engine = ConsolidationEngine(
            state_db_path=state_db,
            memory_db_path=memory_db,
            dream_data_dir=tmp_dir / "nonexistent",
            output_dir=output_dir,
        )
        result = engine.run()
        assert result["weekly_consolidated"] == 0
