"""Tests for EpisodicMemory summary generation methods.

Covers:
  1. generate_monthly_summary — from episodes, no-op empty, upsert, custom synthesizer
  2. generate_yearly_summary — from monthly summaries, skip <2, upsert, custom synthesizer
  3. consolidate_monthly — previous month computation, January edge case
  4. consolidate_yearly — previous year computation
  5. Integration: episode → monthly → yearly pipeline
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))

from episodic_memory import (
    EpisodicMemory,
    MonthlySummary,
    YearlySummary,
    Episode,
)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test_summary_gen.db"


@pytest.fixture
def em(db_path):
    mem = EpisodicMemory(db_path)
    mem.initialize()
    yield mem
    mem.close()


def _seed_april_episodes(em: EpisodicMemory) -> None:
    """Create two episodes in April 2026."""
    em.create_episode(
        week_start="2026-04-06",
        title="Identity system built",
        narrative="Implemented the immutable identity anchor using SQLite.",
        topics=["identity", "sqlite"],
        key_decisions=["separate DB for identity"],
    )
    em.create_episode(
        week_start="2026-04-13",
        title="Episodic memory designed",
        narrative="Designed weekly episodes with FTS5 search.",
        topics=["memory", "fts5"],
        key_decisions=["weekly granularity", "FTS5 for search"],
    )


def _seed_monthly_summaries(conn, year: int, months: list[int]) -> None:
    """Insert raw monthly summary rows for the given months."""
    for m in months:
        conn.execute(
            """INSERT OR REPLACE INTO monthly_summaries (year, month, summary, highlights)
               VALUES (?, ?, ?, ?)""",
            (year, m, f"Summary for month {m}", json.dumps([f"highlight_{m}_a", f"highlight_{m}_b"])),
        )
    conn.commit()


# -- 1. generate_monthly_summary -------------------------------------------

class TestGenerateMonthlySummary:
    def test_generates_from_episodes(self, em):
        """Monthly summary is generated from episodes in that month."""
        _seed_april_episodes(em)
        result = em.generate_monthly_summary(2026, 4)

        assert result is not None
        assert isinstance(result, MonthlySummary)
        assert result.year == 2026
        assert result.month == 4
        assert result.id > 0

        # Summary should mention episode content
        assert "Identity system built" in result.summary
        assert "Episodic memory designed" in result.summary
        assert len(result.summary) > 50  # not empty

    def test_highlights_extracted(self, em):
        """Highlights are drawn from topics and key_decisions."""
        _seed_april_episodes(em)
        result = em.generate_monthly_summary(2026, 4)

        assert len(result.highlights) >= 2
        assert len(result.highlights) <= 5
        # Should include some of the topics/decisions
        all_source = ["identity", "sqlite", "memory", "fts5",
                      "separate DB for identity", "weekly granularity", "FTS5 for search"]
        matched = [h for h in result.highlights if h in all_source]
        assert len(matched) >= 2

    def test_no_episodes_returns_none(self, em):
        """No episodes for the month → returns None (no-op)."""
        result = em.generate_monthly_summary(2026, 12)
        assert result is None

    def test_upsert_on_regenerate(self, em):
        """Re-generating for the same month replaces the existing row."""
        _seed_april_episodes(em)
        first = em.generate_monthly_summary(2026, 4)
        second = em.generate_monthly_summary(2026, 4)

        assert second is not None
        # Same year+month, so INSERT OR REPLACE overwrites
        assert second.year == 2026
        assert second.month == 4
        # Content should be identical (same episodes, same synthesis)
        assert second.summary == first.summary
        assert second.highlights == first.highlights

    def test_custom_synthesizer(self, em):
        """A custom synthesizer callable is used instead of default."""
        _seed_april_episodes(em)

        def custom_synth(episodes: list[Episode]) -> tuple[str, list[str]]:
            titles = [e.title for e in episodes]
            return f"Custom: {len(episodes)} episodes — {', '.join(titles)}", ["custom_highlight"]

        result = em.generate_monthly_summary(2026, 4, synthesizer=custom_synth)
        assert result is not None
        assert result.summary.startswith("Custom:")
        assert "2 episodes" in result.summary
        assert result.highlights == ["custom_highlight"]

    def test_persisted_in_db(self, em):
        """Summary is actually stored in the monthly_summaries table."""
        _seed_april_episodes(em)
        em.generate_monthly_summary(2026, 4)

        conn = em._get_conn()
        row = conn.execute(
            "SELECT * FROM monthly_summaries WHERE year=2026 AND month=4"
        ).fetchone()
        assert row is not None
        assert row["year"] == 2026
        assert row["month"] == 4
        assert len(row["summary"]) > 0

    def test_single_episode_month(self, em):
        """Works with a single episode in the month."""
        em.create_episode(
            week_start="2026-03-02",
            title="Solo week",
            narrative="Only episode this month.",
            topics=["solo"],
        )
        result = em.generate_monthly_summary(2026, 3)
        assert result is not None
        assert "Solo week" in result.summary

    def test_episodes_from_different_months_ignored(self, em):
        """Only episodes whose week_start matches the target month are included."""
        em.create_episode(week_start="2026-04-06", title="April ep")
        em.create_episode(week_start="2026-05-04", title="May ep")

        result = em.generate_monthly_summary(2026, 4)
        assert result is not None
        assert "April ep" in result.summary
        assert "May ep" not in result.summary


# -- 2. generate_yearly_summary --------------------------------------------

class TestGenerateYearlySummary:
    def test_generates_from_monthly_summaries(self, em):
        """Yearly summary aggregates monthly summaries."""
        conn = em._get_conn()
        _seed_monthly_summaries(conn, 2026, [1, 2, 3])

        result = em.generate_yearly_summary(2026)
        assert result is not None
        assert isinstance(result, YearlySummary)
        assert result.year == 2026
        assert result.id > 0
        assert "2026" in result.summary
        assert "3 month" in result.summary

    def test_fewer_than_2_months_skips(self, em):
        """Fewer than 2 monthly summaries → returns None."""
        conn = em._get_conn()
        _seed_monthly_summaries(conn, 2026, [4])

        result = em.generate_yearly_summary(2026)
        assert result is None

    def test_exactly_2_months_works(self, em):
        """Exactly 2 monthly summaries is the minimum threshold."""
        conn = em._get_conn()
        _seed_monthly_summaries(conn, 2026, [3, 6])

        result = em.generate_yearly_summary(2026)
        assert result is not None
        assert "2 month" in result.summary

    def test_no_monthly_summaries_skips(self, em):
        """No monthly summaries at all → returns None."""
        result = em.generate_yearly_summary(2026)
        assert result is None

    def test_highlights_aggregated(self, em):
        """Highlights from all months are collected (up to 10)."""
        conn = em._get_conn()
        _seed_monthly_summaries(conn, 2026, [1, 2, 3, 4, 5, 6])

        result = em.generate_yearly_summary(2026)
        assert result is not None
        assert len(result.highlights) >= 4  # at least 2 per month, some unique
        assert len(result.highlights) <= 10

    def test_upsert_on_regenerate(self, em):
        """Re-generating replaces the existing yearly row."""
        conn = em._get_conn()
        _seed_monthly_summaries(conn, 2026, [1, 2, 3])

        first = em.generate_yearly_summary(2026)
        second = em.generate_yearly_summary(2026)
        assert second is not None
        assert second.summary == first.summary

    def test_custom_synthesizer(self, em):
        """Custom synthesizer replaces default yearly aggregation."""
        conn = em._get_conn()
        _seed_monthly_summaries(conn, 2026, [1, 2])

        def custom_synth(summaries):
            return f"Custom year with {len(summaries)} months", ["year_custom"]

        result = em.generate_yearly_summary(2026, synthesizer=custom_synth)
        assert result is not None
        assert "Custom year" in result.summary
        assert result.highlights == ["year_custom"]

    def test_persisted_in_db(self, em):
        """Summary is stored in yearly_summaries table."""
        conn = em._get_conn()
        _seed_monthly_summaries(conn, 2026, [1, 2, 3])
        em.generate_yearly_summary(2026)

        row = conn.execute(
            "SELECT * FROM yearly_summaries WHERE year=2026"
        ).fetchone()
        assert row is not None
        assert row["year"] == 2026

    def test_monthly_summaries_from_different_year_ignored(self, em):
        """Only monthly summaries for the target year are used."""
        conn = em._get_conn()
        _seed_monthly_summaries(conn, 2025, [1, 2])
        _seed_monthly_summaries(conn, 2026, [3, 4])

        result = em.generate_yearly_summary(2026)
        assert result is not None
        assert "2 month" in result.summary


# -- 3. consolidate_monthly ------------------------------------------------

class TestConsolidateMonthly:
    def test_previous_month_from_may(self, em):
        """From May, consolidates April."""
        em.create_episode(week_start="2026-04-06", title="April ep")
        result = em.consolidate_monthly(reference_date=date(2026, 5, 15))

        assert result is not None
        assert result.year == 2026
        assert result.month == 4

    def test_january_wraps_to_december(self, em):
        """From January, consolidates December of prior year."""
        em.create_episode(week_start="2025-12-01", title="Dec ep")
        result = em.consolidate_monthly(reference_date=date(2026, 1, 10))

        assert result is not None
        assert result.year == 2025
        assert result.month == 12

    def test_no_episodes_returns_none(self, em):
        """No episodes for previous month → None."""
        result = em.consolidate_monthly(reference_date=date(2026, 6, 1))
        assert result is None

    def test_uses_today_when_no_date(self, em):
        """Without reference_date, uses today's date."""
        # We can't easily test the exact month without mocking date.today(),
        # but we can verify it doesn't crash
        result = em.consolidate_monthly()
        # Should be None (no episodes) or a valid MonthlySummary
        assert result is None or isinstance(result, MonthlySummary)


# -- 4. consolidate_yearly -------------------------------------------------

class TestConsolidateYearly:
    def test_previous_year(self, em):
        """Consolidates the year before the reference date."""
        conn = em._get_conn()
        _seed_monthly_summaries(conn, 2025, [1, 2, 3])

        result = em.consolidate_yearly(reference_date=date(2026, 3, 15))
        assert result is not None
        assert result.year == 2025

    def test_insufficient_months_returns_none(self, em):
        """Fewer than 2 monthly summaries for previous year → None."""
        conn = em._get_conn()
        _seed_monthly_summaries(conn, 2025, [6])

        result = em.consolidate_yearly(reference_date=date(2026, 1, 1))
        assert result is None

    def test_uses_today_when_no_date(self, em):
        """Without reference_date, uses today's date."""
        result = em.consolidate_yearly()
        assert result is None or isinstance(result, YearlySummary)


# -- 5. Integration: full pipeline ----------------------------------------

class TestSummaryPipeline:
    def test_episode_to_monthly_to_yearly(self, em):
        """Full pipeline: create episodes → monthly summary → yearly summary."""
        # Seed episodes across two months
        em.create_episode(
            week_start="2026-03-02", title="March week 1",
            narrative="Setup work", topics=["setup"],
            key_decisions=["use SQLite"],
        )
        em.create_episode(
            week_start="2026-03-09", title="March week 2",
            narrative="More setup", topics=["architecture"],
        )
        em.create_episode(
            week_start="2026-04-06", title="April week 1",
            narrative="Build phase", topics=["identity"],
            key_decisions=["separate DB"],
        )
        em.create_episode(
            week_start="2026-04-13", title="April week 2",
            narrative="More building", topics=["memory"],
        )

        # Generate monthly summaries
        mar = em.generate_monthly_summary(2026, 3)
        apr = em.generate_monthly_summary(2026, 4)
        assert mar is not None
        assert apr is not None

        # Generate yearly summary
        yearly = em.generate_yearly_summary(2026)
        assert yearly is not None
        assert yearly.year == 2026
        assert "2026" in yearly.summary
        assert "2 month" in yearly.summary
        assert len(yearly.highlights) >= 2

    def test_zoom_navigation_includes_generated_summaries(self, em):
        """Generated summaries appear in zoom_navigation output."""
        em.create_episode(
            week_start="2026-03-02", title="March ep",
            narrative="March work on memory.", topics=["memory"],
            key_decisions=["weekly episodes"],
        )
        em.create_episode(
            week_start="2026-04-06", title="April ep",
            narrative="April identity work.", topics=["identity"],
            key_decisions=["separate DB"],
        )
        em.generate_monthly_summary(2026, 3)
        em.generate_monthly_summary(2026, 4)
        em.generate_yearly_summary(2026)

        tree = em.zoom_navigation()
        assert len(tree) >= 1
        year_node = [y for y in tree if y.year == 2026][0]
        assert len(year_node.summary) > 0
        assert len(year_node.highlights) > 0

        april_node = [m for m in year_node.months if m.month == 4][0]
        assert len(april_node.summary) > 0

    def test_regenerate_monthly_then_yearly(self, em):
        """Re-generating monthly then yearly produces consistent results."""
        em.create_episode(
            week_start="2026-01-05", title="Jan ep", topics=["a"],
        )
        em.create_episode(
            week_start="2026-02-02", title="Feb ep", topics=["b"],
        )

        em.generate_monthly_summary(2026, 1)
        em.generate_monthly_summary(2026, 2)
        yearly1 = em.generate_yearly_summary(2026)

        # Regenerate monthly for January with different content
        em.create_episode(
            week_start="2026-01-12", title="Jan ep 2", topics=["c"],
        )
        em.generate_monthly_summary(2026, 1)  # re-generate
        yearly2 = em.generate_yearly_summary(2026)  # re-generate

        # Yearly should reflect updated monthly
        assert yearly2 is not None
        assert "3 month" in yearly2.summary or "Jan ep 2" in yearly2.summary
