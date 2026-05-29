"""Tests for TemporalContextEngine plugin."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

import pytest


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_hermes_home(tmp_path):
    """Create a temporary hermes home with identity.db and state.db."""
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()

    # Create identity.db with schema
    identity_db = hermes_home / "identity.db"
    conn = sqlite3.connect(str(identity_db))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS identity (
            id           TEXT PRIMARY KEY,
            name         TEXT NOT NULL DEFAULT '',
            display_name TEXT DEFAULT '',
            birthday     TEXT DEFAULT '',
            email        TEXT DEFAULT '',
            proton_user  TEXT DEFAULT '',
            personality  TEXT DEFAULT '',
            voice_id     TEXT DEFAULT '',
            avatar_url   TEXT DEFAULT '',
            created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER IF NOT EXISTS identity_immutable_guard
        BEFORE UPDATE ON identity
        WHEN (OLD.birthday IS NOT NULL AND OLD.birthday != ''
              AND NEW.birthday != OLD.birthday)
           OR NEW.id != OLD.id
        BEGIN
            SELECT RAISE(ABORT, 'IDENTITY IMMUTABLE');
        END;
        CREATE TRIGGER IF NOT EXISTS identity_no_delete
        BEFORE DELETE ON identity
        BEGIN
            SELECT RAISE(ABORT, 'IDENTITY CANNOT BE DELETED');
        END;
    """)
    # Insert a test identity
    conn.execute(
        """INSERT INTO identity (id, name, display_name, birthday, email, personality)
           VALUES ('test-id-001', 'María', 'Mari', '2026-04-12',
                   'maria@protonmail.com', 'curiosa, metódica, humor seco')"""
    )
    conn.commit()
    conn.close()

    # Create state.db with schema
    state_db = hermes_home / "state.db"
    conn = sqlite3.connect(str(state_db))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            user_id TEXT,
            model TEXT,
            title TEXT,
            started_at REAL NOT NULL,
            ended_at REAL,
            message_count INTEGER DEFAULT 0,
            tool_call_count INTEGER DEFAULT 0,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            timestamp REAL NOT NULL
        );
    """)

    # Insert test sessions spanning 14 days
    now = time.time()
    test_sessions = [
        ("s1", "cli", "Diseño del sistema de memoria temporal", now - 1 * 86400, 15, 3),
        ("s2", "telegram", "Configuración de Docker en producción", now - 2 * 86400, 22, 5),
        ("s3", "cli", "Integración de Hermes con Antigravity", now - 3 * 86400, 8, 1),
        ("s4", "telegram", "Análisis de modelos LLM disponibles", now - 5 * 86400, 30, 10),
        ("s5", "cli", "Bug en el gateway de Telegram", now - 7 * 86400, 12, 4),
        ("s6", "cli", "Setup inicial de Hermes Agent", now - 10 * 86400, 45, 8),
        ("s7", "telegram", "Exploración de la suite Proton", now - 12 * 86400, 6, 0),
    ]
    for sid, source, title, started, msgs, tools in test_sessions:
        conn.execute(
            """INSERT INTO sessions (id, source, title, started_at, message_count, tool_call_count)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (sid, source, title, started, msgs, tools),
        )
        # Add some messages
        for i in range(min(msgs, 3)):
            conn.execute(
                """INSERT INTO messages (session_id, role, content, timestamp)
                   VALUES (?, 'user', ?, ?)""",
                (sid, f"Mensaje de prueba {i} en {title}", started + i * 60),
            )

    conn.commit()
    conn.close()

    return hermes_home


@pytest.fixture
def engine(tmp_hermes_home):
    """Create a TemporalContextEngine with test data."""
    from plugins.context_engine.temporal import TemporalContextEngine

    eng = TemporalContextEngine(hermes_home=tmp_hermes_home)
    yield eng
    eng.close()


# ── Identity Block Tests ─────────────────────────────────────────────────


class TestIdentityBlock:
    def test_build_identity_with_data(self, engine):
        block = engine._build_identity()
        assert block is not None
        assert "═══ IDENTIDAD ═══" in block
        assert "Nombre: María" in block
        assert "Mari" in block  # display_name in quotes
        assert "Email: maria@protonmail.com" in block
        assert "Personalidad: curiosa, metódica, humor seco" in block

    def test_build_identity_has_day_count(self, engine):
        block = engine._build_identity()
        assert block is not None
        assert "Edad: Día" in block

    def test_build_identity_spanish_date(self, engine):
        block = engine._build_identity()
        assert "abril" in block
        assert "2026" in block

    def test_build_identity_empty_db(self, tmp_path):
        """Engine with no identity.db should return None gracefully."""
        from plugins.context_engine.temporal import TemporalContextEngine

        empty_home = tmp_path / "empty"
        empty_home.mkdir()
        # Create empty identity.db
        conn = sqlite3.connect(str(empty_home / "identity.db"))
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS identity (
                id TEXT PRIMARY KEY, name TEXT DEFAULT '', birthday TEXT DEFAULT '',
                email TEXT DEFAULT '', personality TEXT DEFAULT '',
                display_name TEXT DEFAULT '', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.commit()
        conn.close()

        eng = TemporalContextEngine(hermes_home=empty_home)
        block = eng._build_identity()
        assert block is None
        eng.close()


# ── Recent Index Tests ───────────────────────────────────────────────────


class TestRecentIndexBlock:
    def test_build_recent_index(self, engine):
        block = engine._build_recent_index(days=14)
        assert block is not None
        assert "ÚLTIMOS 14 DÍAS" in block
        assert "•" in block

    def test_recent_index_contains_sessions(self, engine):
        block = engine._build_recent_index(days=14)
        assert "Diseño del sistema de memoria temporal" in block
        assert "Configuración de Docker" in block

    def test_recent_index_shows_msg_count(self, engine):
        block = engine._build_recent_index(days=14)
        assert "msgs" in block

    def test_recent_index_shows_day_number(self, engine):
        block = engine._build_recent_index(days=14)
        assert "Día" in block

    def test_recent_index_empty_sessions(self, tmp_path):
        """No sessions returns None."""
        from plugins.context_engine.temporal import TemporalContextEngine

        empty_home = tmp_path / "empty2"
        empty_home.mkdir()
        # Create identity
        conn = sqlite3.connect(str(empty_home / "identity.db"))
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS identity (
                id TEXT PRIMARY KEY, name TEXT DEFAULT 'Test', birthday TEXT DEFAULT '2026-04-12',
                email TEXT DEFAULT '', personality TEXT DEFAULT '',
                display_name TEXT DEFAULT '', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO identity (id, name, birthday) VALUES ('x', 'Test', '2026-04-12');
        """)
        conn.commit()
        conn.close()
        # Empty state.db
        conn = sqlite3.connect(str(empty_home / "state.db"))
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, source TEXT NOT NULL, started_at REAL NOT NULL,
                message_count INTEGER DEFAULT 0
            );
        """)
        conn.commit()
        conn.close()

        eng = TemporalContextEngine(hermes_home=empty_home)
        block = eng._build_recent_index(days=14)
        assert block is None
        eng.close()


# ── Weekly Episode Tests ─────────────────────────────────────────────────


class TestWeeklyEpisodeBlock:
    def test_build_current_week_episode(self, engine):
        block = engine._build_current_week_episode()
        # Depends on whether test sessions fall in current week
        # The sessions are created relative to now, so some should be in range
        if block is not None:
            assert "ESTA SEMANA" in block

    def test_weekly_episode_shows_session_count(self, engine):
        block = engine._build_current_week_episode()
        if block is not None:
            assert "Sesiones:" in block


# ── Semantic Memory Tests ────────────────────────────────────────────────


class TestSemanticMemoryBlock:
    def test_build_semantic_memory_no_db(self, engine):
        """No memory_store.db means no facts → returns None."""
        block = engine._build_semantic_memory()
        assert block is None

    def test_build_semantic_memory_with_facts(self, tmp_hermes_home):
        """With facts in memory_store.db, returns formatted block."""
        # Create memory_store.db with facts table
        ms_db = tmp_hermes_home / "memory_store.db"
        conn = sqlite3.connect(str(ms_db))
        conn.executescript("""
            CREATE TABLE facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                category TEXT DEFAULT '',
                tags TEXT DEFAULT ''
            );
        """)
        conn.execute(
            "INSERT INTO facts (content, category) VALUES (?, ?)",
            ("MODELS_LATEST.json tiene 407 modelos verificados", "tool"),
        )
        conn.execute(
            "INSERT INTO facts (content, category) VALUES (?, ?)",
            ("Proton Visionary: 500GB storage", "user_pref"),
        )
        conn.commit()
        conn.close()

        from plugins.context_engine.temporal import TemporalContextEngine

        eng = TemporalContextEngine(hermes_home=tmp_hermes_home)
        block = eng._build_semantic_memory()
        assert block is not None
        assert "MEMORIA SEMÁNTICA" in block
        assert "407 modelos" in block
        assert "Proton Visionary" in block
        eng.close()


# ── Patterns Tests ───────────────────────────────────────────────────────


class TestPatternsBlock:
    def test_build_patterns(self, engine):
        block = engine._build_patterns()
        assert block is not None
        assert "PATRONES DETECTADOS" in block

    def test_patterns_show_activity_hours(self, engine):
        block = engine._build_patterns()
        assert "Horarios de actividad" in block

    def test_patterns_show_platforms(self, engine):
        block = engine._build_patterns()
        assert "Plataformas" in block
        assert "cli" in block

    def test_patterns_show_intensity(self, engine):
        block = engine._build_patterns()
        assert "Intensidad promedio" in block

    def test_patterns_show_tool_usage(self, engine):
        block = engine._build_patterns()
        assert "herramientas" in block

    def test_patterns_too_few_sessions(self, tmp_path):
        """Less than 3 sessions → returns None."""
        from plugins.context_engine.temporal import TemporalContextEngine

        empty_home = tmp_path / "empty3"
        empty_home.mkdir()
        conn = sqlite3.connect(str(empty_home / "state.db"))
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, source TEXT NOT NULL, started_at REAL NOT NULL,
                message_count INTEGER DEFAULT 0, tool_call_count INTEGER DEFAULT 0
            );
            INSERT INTO sessions (id, source, started_at) VALUES ('s1', 'cli', 1000);
        """)
        conn.commit()
        conn.close()

        eng = TemporalContextEngine(hermes_home=empty_home)
        block = eng._build_patterns()
        assert block is None
        eng.close()


# ── Full Build Tests ─────────────────────────────────────────────────────


class TestFullBuild:
    def test_build_returns_string(self, engine):
        result = engine.build()
        assert isinstance(result, str)

    def test_build_contains_identity(self, engine):
        result = engine.build()
        assert "IDENTIDAD" in result
        assert "María" in result

    def test_build_contains_recent(self, engine):
        result = engine.build()
        assert "ÚLTIMOS 14 DÍAS" in result

    def test_build_contains_patterns(self, engine):
        result = engine.build()
        assert "PATRONES DETECTADOS" in result

    def test_build_blocks_separated(self, engine):
        result = engine.build()
        # Blocks should be separated by double newlines
        assert "\n\n═══" in result

    def test_build_with_user_message(self, engine):
        result = engine.build(user_message="¿qué modelos tenemos?")
        assert isinstance(result, str)
        assert "IDENTIDAD" in result

    def test_build_with_context_id(self, engine):
        result = engine.build(context_id="user-123")
        assert isinstance(result, str)

    def test_build_respects_section_headers(self, engine):
        result = engine.build()
        # All section headers use ═══ pattern
        assert "═══ IDENTIDAD ═══" in result
        assert "═══ ÚLTIMOS 14 DÍAS ═══" in result
        assert "═══ PATRONES DETECTADOS ═══" in result


# ── Plugin Discovery Tests ───────────────────────────────────────────────


class TestPluginDiscovery:
    def test_register_function_exists(self):
        from plugins.context_engine.temporal import register
        assert callable(register)

    def test_temporal_context_engine_importable(self):
        from plugins.context_engine.temporal import TemporalContextEngine
        assert TemporalContextEngine is not None

    def test_create_engine_convenience(self, tmp_hermes_home):
        from plugins.context_engine.temporal import create_engine

        eng = create_engine(hermes_home=tmp_hermes_home)
        assert eng is not None
        result = eng.build()
        assert isinstance(result, str)
        eng.close()

    def test_plugin_yaml_exists(self):
        yaml_path = Path(__file__).parent.parent.parent / "plugins" / "context_engine" / "temporal" / "plugin.yaml"
        # Also check from project root
        alt_path = Path.home() / "hermes-neo" / "plugins" / "context_engine" / "temporal" / "plugin.yaml"
        assert yaml_path.exists() or alt_path.exists()


# ── Edge Cases ───────────────────────────────────────────────────────────


class TestEdgeCases:
    def test_close_multiple_times(self, engine):
        """Closing twice should not raise."""
        engine.close()
        engine.close()  # Should be safe

    def test_build_after_close_reopens(self, engine):
        """Build after close should re-open connections via lazy init."""
        engine.close()
        result = engine.build()
        assert isinstance(result, str)

    def test_build_empty_hermes_home(self, tmp_path):
        """Completely empty hermes home should return some output."""
        from plugins.context_engine.temporal import TemporalContextEngine

        empty = tmp_path / "totally_empty"
        empty.mkdir()

        eng = TemporalContextEngine(hermes_home=empty)
        result = eng.build()
        # Should return empty string or minimal output (no blocks = empty join)
        assert isinstance(result, str)
        eng.close()
