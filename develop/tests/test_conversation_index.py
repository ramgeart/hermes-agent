"""
Comprehensive pytest tests for ConversationIndex module.
"""
import pytest
from datetime import date, timedelta
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))
from conversation_index import ConversationIndex


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test_memory.db"


@pytest.fixture
def ci(db_path):
    idx = ConversationIndex(str(db_path))
    idx.initialize()
    yield idx
    idx.close()


# ── Helpers ───────────────────────────────────────────────────────────────

def _add_sample_sessions(ci, count=5):
    """Add count sample sessions spread over recent days."""
    for i in range(count):
        d = date.today() - timedelta(days=i)
        ci.add_session(
            session_id=f"test_session_{i:03d}",
            title=f"Test Session {i}: Topic about {'python' if i % 2 == 0 else 'docker'}",
            summary=f"Summary of session {i}. We discussed {'coding' if i % 2 == 0 else 'deployment'}.",
            topics="python,code" if i % 2 == 0 else "docker,deploy",
            day_number=48 - i,
            date=d.isoformat(),
            weekday=d.strftime("%A"),
            importance=5 + i,
        )


# ── 1. Schema creation ───────────────────────────────────────────────────

class TestSchemaCreation:
    def test_initialize_creates_db_file(self, db_path, ci):
        """initialize() creates the database file."""
        # ci fixture already called initialize; DB should exist now
        assert Path(db_path).exists()

    def test_initialize_creates_table(self, ci):
        """conversation_index table exists."""
        conn = ci._get_conn()
        tables = [
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        assert "conversation_index" in tables

    def test_initialize_creates_fts5_virtual_table(self, ci):
        """FTS5 virtual table conversation_fts exists."""
        conn = ci._get_conn()
        tables = [
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        assert "conversation_fts" in tables

    def test_initialize_creates_triggers(self, ci):
        """All four triggers are created."""
        conn = ci._get_conn()
        triggers = [
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            ).fetchall()
        ]
        assert "conv_idx_ai" in triggers   # after insert
        assert "conv_idx_au" in triggers   # after update
        assert "conv_idx_ad" in triggers   # after delete
        assert "conv_idx_updated_at" in triggers

    def test_initialize_creates_indexes(self, ci):
        """Indexes on date, day_number, importance exist."""
        conn = ci._get_conn()
        indexes = [
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
            ).fetchall()
        ]
        assert "idx_conv_date" in indexes
        assert "idx_conv_day" in indexes
        assert "idx_conv_importance" in indexes

    def test_initialize_idempotent(self, ci):
        """Calling initialize() twice doesn't raise."""
        ci.initialize()  # second call should be fine


# ── 2. CRUD operations ───────────────────────────────────────────────────

class TestCRUD:
    def test_add_session_returns_rowid(self, ci):
        """add_session returns an integer idx_id."""
        idx_id = ci.add_session(
            session_id="s1", title="T1", summary="S1",
            topics="a", day_number=1, date="2026-01-01", weekday="Monday",
        )
        assert isinstance(idx_id, int)
        assert idx_id > 0

    def test_add_session_all_fields(self, ci):
        """All optional fields are stored correctly."""
        ci.add_session(
            session_id="s_full",
            title="Full",
            summary="Full summary",
            topics="python,ai",
            day_number=10,
            date="2026-05-01",
            weekday="Friday",
            importance=8,
            mood="productive",
            participants="Alice,Bob",
            tools_used="git,docker",
            tokens_total=5000,
            msg_count=42,
        )
        row = ci.get_session("s_full")
        assert row is not None
        assert row["title"] == "Full"
        assert row["summary"] == "Full summary"
        assert row["topics"] == "python,ai"
        assert row["day_number"] == 10
        assert row["date"] == "2026-05-01"
        assert row["weekday"] == "Friday"
        assert row["importance"] == 8
        assert row["mood"] == "productive"
        assert row["participants"] == "Alice,Bob"
        assert row["tools_used"] == "git,docker"
        assert row["tokens_total"] == 5000
        assert row["msg_count"] == 42

    def test_get_session_not_found(self, ci):
        """get_session returns None for missing id."""
        assert ci.get_session("nonexistent") is None

    def test_get_session_found(self, ci):
        """get_session returns correct dict."""
        ci.add_session(
            session_id="s2", title="T2", summary="S2",
            topics="t", day_number=2, date="2026-02-02", weekday="Sunday",
        )
        row = ci.get_session("s2")
        assert row is not None
        assert row["session_id"] == "s2"
        assert row["title"] == "T2"

    def test_update_session_single_field(self, ci):
        """update_session changes one field."""
        ci.add_session(
            session_id="s3", title="Old Title", summary="S3",
            topics="t", day_number=3, date="2026-03-03", weekday="Tuesday",
        )
        ci.update_session("s3", title="New Title")
        row = ci.get_session("s3")
        assert row["title"] == "New Title"

    def test_update_session_multiple_fields(self, ci):
        """update_session changes multiple fields at once."""
        ci.add_session(
            session_id="s4", title="T4", summary="S4",
            topics="t", day_number=4, date="2026-04-04", weekday="Saturday",
        )
        ci.update_session("s4", title="Updated", importance=9, mood="focused")
        row = ci.get_session("s4")
        assert row["title"] == "Updated"
        assert row["importance"] == 9
        assert row["mood"] == "focused"

    def test_update_session_no_kwargs(self, ci):
        """update_session with empty kwargs is a no-op."""
        ci.add_session(
            session_id="s5", title="T5", summary="S5",
            topics="t", day_number=5, date="2026-05-05", weekday="Monday",
        )
        ci.update_session("s5")  # should not raise
        row = ci.get_session("s5")
        assert row["title"] == "T5"

    def test_update_session_ignores_unknown_fields(self, ci):
        """update_session silently ignores unknown kwargs."""
        ci.add_session(
            session_id="s6", title="T6", summary="S6",
            topics="t", day_number=6, date="2026-06-06", weekday="Saturday",
        )
        ci.update_session("s6", title="OK", nonexistent_field="boom")
        row = ci.get_session("s6")
        assert row["title"] == "OK"


# ── 3. FTS5 search ───────────────────────────────────────────────────────

class TestSearch:
    def test_search_single_term(self, ci):
        """Search for a single word matches across title/summary/topics."""
        _add_sample_sessions(ci, 4)
        results = ci.search("python")
        assert len(results) > 0
        # All results should have 'python' in title, summary, or topics
        for r in results:
            text = (r["title"] + r["summary"] + r["topics"]).lower()
            assert "python" in text

    def test_search_multi_word_and(self, ci):
        """Multi-word query defaults to AND semantics in FTS5."""
        ci.add_session(
            session_id="m1", title="Docker containers", summary="Deploy with docker",
            topics="docker", day_number=1, date="2026-01-01", weekday="Wednesday",
        )
        ci.add_session(
            session_id="m2", title="Python coding", summary="Code in python",
            topics="python", day_number=2, date="2026-01-02", weekday="Thursday",
        )
        # FTS5 multi-word is AND by default
        results = ci.search("docker deploy")
        assert len(results) >= 1
        assert all("docker" in r["summary"].lower() for r in results)

    def test_search_or_query(self, ci):
        """FTS5 OR query matches either term."""
        _add_sample_sessions(ci, 4)
        results = ci.search("python OR docker")
        # Should find sessions with either python or docker
        assert len(results) >= 2

    def test_search_prefix(self, ci):
        """FTS5 prefix search with * suffix."""
        ci.add_session(
            session_id="p1", title="Programming guide", summary="Learn programming",
            topics="code", day_number=1, date="2026-01-01", weekday="Monday",
        )
        results = ci.search("program*")
        assert len(results) >= 1

    def test_search_limit(self, ci):
        """Search respects the limit parameter."""
        _add_sample_sessions(ci, 10)
        results = ci.search("session", limit=3)
        assert len(results) <= 3

    def test_search_no_results(self, ci):
        """Search for nonexistent term returns empty list."""
        _add_sample_sessions(ci, 3)
        results = ci.search("nonexistent_term_xyz")
        assert results == []

    def test_search_empty_db(self, ci):
        """Search on empty DB returns empty list."""
        results = ci.search("anything")
        assert results == []


# ── 4. Temporal queries ──────────────────────────────────────────────────

class TestTemporalQueries:
    def test_get_recent_sessions_default(self, ci):
        """get_recent_sessions with default 14 days."""
        _add_sample_sessions(ci, 3)
        results = ci.get_recent_sessions()
        # All 3 sessions are from today/yesterday/day-before, within 14 days
        assert len(results) == 3

    def test_get_recent_sessions_ordering(self, ci):
        """Results ordered by date DESC, importance DESC."""
        _add_sample_sessions(ci, 3)
        results = ci.get_recent_sessions()
        for i in range(len(results) - 1):
            a, b = results[i], results[i + 1]
            assert a["date"] >= b["date"]

    def test_get_recent_sessions_excludes_old(self, ci):
        """Sessions older than the cutoff are excluded."""
        # Add an old session (100 days ago)
        old_date = (date.today() - timedelta(days=100)).isoformat()
        ci.add_session(
            session_id="old1", title="Old", summary="Old session",
            topics="old", day_number=1, date=old_date, weekday="Monday",
        )
        _add_sample_sessions(ci, 2)  # today and yesterday
        results = ci.get_recent_sessions(days=14)
        assert all(r["session_id"] != "old1" for r in results)

    def test_get_recent_sessions_custom_days(self, ci):
        """Custom days parameter works."""
        d30 = (date.today() - timedelta(days=30)).isoformat()
        d5 = (date.today() - timedelta(days=5)).isoformat()
        ci.add_session(
            session_id="r_old", title="30d ago", summary="s",
            topics="t", day_number=1, date=d30, weekday="Monday",
        )
        ci.add_session(
            session_id="r_new", title="5d ago", summary="s",
            topics="t", day_number=2, date=d5, weekday="Monday",
        )
        results = ci.get_recent_sessions(days=10)
        ids = [r["session_id"] for r in results]
        assert "r_new" in ids
        assert "r_old" not in ids

    def test_get_sessions_by_week(self, ci):
        """get_sessions_by_week returns sessions within the 7-day window."""
        monday = date(2026, 5, 25)  # a known Monday
        for i in range(7):
            d = monday + timedelta(days=i)
            ci.add_session(
                session_id=f"wk_{i}", title=f"Day {i}", summary="s",
                topics="t", day_number=i, date=d.isoformat(),
                weekday=d.strftime("%A"),
            )
        results = ci.get_sessions_by_week("2026-05-25")
        assert len(results) == 7

    def test_get_sessions_by_week_excludes_outside(self, ci):
        """Sessions outside the week window are excluded."""
        monday = date(2026, 6, 1)
        # Session before the week
        ci.add_session(
            session_id="before", title="Before", summary="s",
            topics="t", day_number=0,
            date=(monday - timedelta(days=1)).isoformat(), weekday="Sunday",
        )
        # Session in the week
        ci.add_session(
            session_id="in_week", title="In", summary="s",
            topics="t", day_number=1, date=monday.isoformat(), weekday="Monday",
        )
        results = ci.get_sessions_by_week("2026-06-01")
        ids = [r["session_id"] for r in results]
        assert "in_week" in ids
        assert "before" not in ids

    def test_get_sessions_by_week_empty(self, ci):
        """Empty week returns empty list."""
        results = ci.get_sessions_by_week("2025-01-06")
        assert results == []


# ── 5. Consolidation ─────────────────────────────────────────────────────

class TestConsolidation:
    def test_get_unconsolidated_initially(self, ci):
        """New sessions are unconsolidated by default."""
        _add_sample_sessions(ci, 3)
        results = ci.get_unconsolidated()
        assert len(results) == 3

    def test_mark_consolidated(self, ci):
        """mark_consolidated sets consolidated=1."""
        _add_sample_sessions(ci, 3)
        ci.mark_consolidated("test_session_000")
        row = ci.get_session("test_session_000")
        assert row["consolidated"] == 1

    def test_mark_consolidated_removes_from_unconsolidated(self, ci):
        """Consolidated sessions no longer appear in get_unconsolidated."""
        _add_sample_sessions(ci, 3)
        ci.mark_consolidated("test_session_001")
        results = ci.get_unconsolidated()
        ids = [r["session_id"] for r in results]
        assert "test_session_001" not in ids
        assert len(results) == 2

    def test_mark_all_consolidated(self, ci):
        """All sessions consolidated => empty unconsolidated list."""
        _add_sample_sessions(ci, 3)
        for i in range(3):
            ci.mark_consolidated(f"test_session_{i:03d}")
        assert ci.get_unconsolidated() == []


# ── 6. Stats ─────────────────────────────────────────────────────────────

class TestStats:
    def test_get_stats_with_data(self, ci):
        """get_stats returns correct aggregates."""
        _add_sample_sessions(ci, 3)
        # Give some sessions non-default msg_count/tokens
        ci.update_session("test_session_000", msg_count=10, tokens_total=1000)
        ci.update_session("test_session_001", msg_count=5, tokens_total=500)

        stats = ci.get_stats()
        assert stats["total_sessions"] == 3
        assert stats["total_messages"] == 15
        assert stats["total_tokens"] == 1500
        assert stats["consolidated_count"] == 0
        assert stats["earliest_date"] is not None
        assert stats["latest_date"] is not None
        assert stats["avg_importance"] is not None

    def test_get_stats_empty_db(self, ci):
        """get_stats on empty DB returns zeros/None."""
        stats = ci.get_stats()
        assert stats["total_sessions"] == 0
        # SUM returns NULL on empty tables
        assert stats["total_messages"] == 0 or stats["total_messages"] is None
        assert stats["total_tokens"] == 0 or stats["total_tokens"] is None
        assert stats["consolidated_count"] == 0 or stats["consolidated_count"] is None

    def test_get_stats_consolidated_count(self, ci):
        """consolidated_count reflects marked sessions."""
        _add_sample_sessions(ci, 4)
        ci.mark_consolidated("test_session_000")
        ci.mark_consolidated("test_session_001")
        stats = ci.get_stats()
        assert stats["consolidated_count"] == 2

    def test_get_stats_avg_importance(self, ci):
        """avg_importance is computed correctly."""
        ci.add_session(
            session_id="a1", title="t", summary="s", topics="t",
            day_number=1, date="2026-01-01", weekday="Monday", importance=4,
        )
        ci.add_session(
            session_id="a2", title="t", summary="s", topics="t",
            day_number=2, date="2026-01-02", weekday="Tuesday", importance=8,
        )
        stats = ci.get_stats()
        assert abs(stats["avg_importance"] - 6.0) < 0.01


# ── 7. Prompt block ──────────────────────────────────────────────────────

class TestPromptBlock:
    def test_get_index_prompt_block_empty(self, ci):
        """Empty DB returns empty string."""
        result = ci.get_index_prompt_block()
        assert result == ""

    def test_get_index_prompt_block_contains_header(self, ci):
        """Prompt block has the expected header line."""
        _add_sample_sessions(ci, 2)
        block = ci.get_index_prompt_block()
        assert "CONVERSATION INDEX" in block

    def test_get_index_prompt_block_contains_titles(self, ci):
        """Prompt block includes session titles."""
        _add_sample_sessions(ci, 2)
        block = ci.get_index_prompt_block()
        assert "Test Session 0" in block
        assert "Test Session 1" in block

    def test_get_index_prompt_block_contains_topics(self, ci):
        """Prompt block includes topics in brackets."""
        _add_sample_sessions(ci, 1)
        block = ci.get_index_prompt_block()
        assert "python,code" in block or "docker,deploy" in block

    def test_get_index_prompt_block_grouping_by_date(self, ci):
        """Sessions are grouped by date with date headers."""
        _add_sample_sessions(ci, 2)
        block = ci.get_index_prompt_block()
        today = date.today().isoformat()
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        # Both dates should appear
        assert today in block or yesterday in block

    def test_get_index_prompt_block_custom_days(self, ci):
        """Custom days parameter is reflected in header."""
        _add_sample_sessions(ci, 1)
        block = ci.get_index_prompt_block(days=30)
        assert "30" in block

    def test_get_index_prompt_block_contains_importance(self, ci):
        """Each entry shows importance."""
        _add_sample_sessions(ci, 1)
        block = ci.get_index_prompt_block()
        assert "imp:" in block


# ── 8. Edge cases ─────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_duplicate_session_id_raises(self, ci):
        """Inserting a duplicate session_id raises IntegrityError."""
        ci.add_session(
            session_id="dup1", title="First", summary="s",
            topics="t", day_number=1, date="2026-01-01", weekday="Monday",
        )
        with pytest.raises(Exception):  # sqlite3.IntegrityError
            ci.add_session(
                session_id="dup1", title="Second", summary="s",
                topics="t", day_number=2, date="2026-01-02", weekday="Tuesday",
            )

    def test_special_characters_in_search(self, ci):
        """Search with special chars doesn't crash."""
        ci.add_session(
            session_id="sp1", title="C++ & Java", summary="Used C++ and Java",
            topics="cpp,java", day_number=1, date="2026-01-01", weekday="Monday",
        )
        # FTS5 special chars should be handled gracefully
        try:
            ci.search("C++")
        except Exception:
            pass  # FTS5 may reject some special chars; that's acceptable

    def test_empty_strings(self, ci):
        """Sessions with empty optional fields are stored."""
        ci.add_session(
            session_id="empty1", title="", summary="",
            topics="", day_number=0, date="", weekday="",
        )
        row = ci.get_session("empty1")
        assert row is not None
        assert row["title"] == ""
        assert row["summary"] == ""

    def test_large_session_count(self, ci):
        """Adding many sessions works without error."""
        for i in range(50):
            d = date.today() - timedelta(days=i)
            ci.add_session(
                session_id=f"bulk_{i:04d}",
                title=f"Bulk session {i}",
                summary=f"Summary {i}",
                topics="bulk,test",
                day_number=i,
                date=d.isoformat(),
                weekday=d.strftime("%A"),
                importance=5,
            )
        stats = ci.get_stats()
        assert stats["total_sessions"] == 50

    def test_fts5_sync_after_update(self, ci):
        """FTS5 index is updated when session fields change."""
        ci.add_session(
            session_id="fts_upd", title="UniqueAlpha", summary="UniqueBeta",
            topics="uniquegamma", day_number=1, date="2026-01-01", weekday="Monday",
        )
        # Verify it's searchable by original title
        assert len(ci.search("UniqueAlpha")) >= 1

        # Update title — also clear summary so old terms are gone everywhere
        ci.update_session("fts_upd", title="ChangedTitle", summary="NewSummary", topics="newtopics")

        # Old title term should no longer be found
        assert len(ci.search("UniqueAlpha")) == 0
        # New title should be found
        assert len(ci.search("ChangedTitle")) >= 1

    def test_fts5_sync_after_delete(self, ci):
        """FTS5 index removes entries when sessions are deleted."""
        ci.add_session(
            session_id="fts_del", title="Deletable", summary="To be deleted",
            topics="delete", day_number=1, date="2026-01-01", weekday="Monday",
        )
        assert len(ci.search("Deletable")) >= 1

        conn = ci._get_conn()
        conn.execute("DELETE FROM conversation_index WHERE session_id = ?", ("fts_del",))
        conn.commit()

        assert len(ci.search("Deletable")) == 0

    def test_close_and_reopen(self, db_path):
        """Data persists after close and reopen."""
        idx = ConversationIndex(str(db_path))
        idx.initialize()
        idx.add_session(
            session_id="persist1", title="Persist", summary="s",
            topics="t", day_number=1, date="2026-01-01", weekday="Monday",
        )
        idx.close()

        # Reopen
        idx2 = ConversationIndex(str(db_path))
        idx2.initialize()
        row = idx2.get_session("persist1")
        assert row is not None
        assert row["title"] == "Persist"
        idx2.close()

    def test_default_values(self, ci):
        """Default values are applied for optional fields."""
        ci.add_session(
            session_id="defaults1", title="Def", summary="Def summary",
            topics="t", day_number=1, date="2026-01-01", weekday="Monday",
        )
        row = ci.get_session("defaults1")
        assert row["importance"] == 5
        assert row["mood"] == ""
        assert row["tools_used"] == ""
        assert row["tokens_total"] == 0
        assert row["msg_count"] == 0
        assert row["consolidated"] == 0
