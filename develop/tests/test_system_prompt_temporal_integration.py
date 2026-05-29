"""Tests for TemporalContextEngine integration in system_prompt.py.

Covers:
  1. Temporal engine used when plugin is registered and returns content
  2. Falls back to manual identity block when engine not available
  3. Falls back to manual identity block when engine returns empty
  4. Falls back when engine raises exception
  5. Falls back when engine name is not "temporal"
  6. Memory store and user profile sections still present with temporal engine
  7. External memory provider still works with temporal engine
  8. Timestamp line always present
  9. Progressive mode called with empty user_message (session-build time)
 10. Manual fallback blocks not added when temporal engine succeeds
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Adjust path to import from the container's codebase
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _make_agent(**overrides):
    """Create a minimal mock agent with sensible defaults."""
    agent = MagicMock()
    agent.load_soul_identity = False
    agent.skip_context_files = True
    agent.valid_tool_names = set()
    agent._tool_use_enforcement = False
    agent.platform = ""
    agent.model = ""
    agent.provider = ""
    agent._memory_store = None
    agent._memory_enabled = False
    agent._user_profile_enabled = False
    agent._memory_manager = None
    agent.pass_session_id = False
    agent.session_id = None
    agent._kanban_worker_guidance = None
    agent.__dict__.update(overrides)
    return agent


# -- 1. Temporal engine used when registered --------------------------------

class TestTemporalEngineUsed:
    """When a TemporalContextEngine plugin is registered, its output is used."""

    def test_temporal_engine_output_in_volatile(self):
        """build_progressive() output appears in the volatile section."""
        mock_engine = MagicMock()
        mock_engine.name = "temporal"
        mock_engine.build_progressive.return_value = (
            "═══ IDENTIDAD ═══\nNombre: María\nEdad: Día 47\n\n"
            "═══ ÚLTIMOS 14 DÍAS ═══\n  • Día 47: Test session"
        )

        agent = _make_agent()

        with patch(
            "hermes_cli.plugins.get_plugin_context_engine",
            return_value=mock_engine,
        ):
            from agent.system_prompt import build_system_prompt_parts
            parts = build_system_prompt_parts(agent)

        assert "═══ IDENTIDAD ═══" in parts["volatile"]
        assert "═══ ÚLTIMOS 14 DÍAS ═══" in parts["volatile"]
        mock_engine.build_progressive.assert_called_once_with(user_message="")

    def test_progressive_mode_used_not_build(self):
        """Integration calls build_progressive() (not build()) by default."""
        mock_engine = MagicMock()
        mock_engine.name = "temporal"
        mock_engine.build_progressive.return_value = "some context"

        agent = _make_agent()

        with patch(
            "hermes_cli.plugins.get_plugin_context_engine",
            return_value=mock_engine,
        ):
            from agent.system_prompt import build_system_prompt_parts
            build_system_prompt_parts(agent)

        mock_engine.build_progressive.assert_called_once_with(user_message="")
        mock_engine.build.assert_not_called()


# -- 2. Falls back to manual blocks when engine not available ----------------

class TestFallbackWhenNoEngine:
    """When no temporal engine is registered, manual identity+conversation blocks are used."""

    def test_fallback_identity_block(self):
        """Identity manager block is injected when no temporal engine."""
        mock_im = MagicMock()
        mock_im.get_identity_prompt_block.return_value = "═══ IDENTIDAD ═══\nNombre: María"

        agent = _make_agent()

        with patch(
            "hermes_cli.plugins.get_plugin_context_engine",
            return_value=None,
        ), patch(
            "agent.identity_manager.get_identity_manager",
            return_value=mock_im,
        ), patch(
            "agent.conversation_index.get_conversation_index",
            return_value=MagicMock(get_recent_conversations=MagicMock(return_value=[])),
        ):
            from agent.system_prompt import build_system_prompt_parts
            parts = build_system_prompt_parts(agent)

        assert "═══ IDENTIDAD ═══" in parts["volatile"]

    def test_fallback_conversation_topics(self):
        """Recent conversation topics are injected when no temporal engine."""
        mock_ci = MagicMock()
        mock_ci.get_recent_conversations.return_value = [
            {"date": "2026-05-29", "title": "Memory system design", "topics": "memory,arch"}
        ]

        agent = _make_agent()

        with patch(
            "hermes_cli.plugins.get_plugin_context_engine",
            return_value=None,
        ), patch(
            "agent.identity_manager.get_identity_manager",
            side_effect=Exception("no identity"),
        ), patch(
            "agent.conversation_index.get_conversation_index",
            return_value=mock_ci,
        ):
            from agent.system_prompt import build_system_prompt_parts
            parts = build_system_prompt_parts(agent)

        assert "RECENT CONVERSATION TOPICS" in parts["volatile"]
        assert "Memory system design" in parts["volatile"]


# -- 3. Falls back when engine returns empty --------------------------------

class TestFallbackWhenEngineEmpty:
    """When build_progressive() returns empty string, fall back to manual blocks."""

    def test_empty_engine_triggers_fallback(self):
        mock_engine = MagicMock()
        mock_engine.name = "temporal"
        mock_engine.build_progressive.return_value = ""

        mock_im = MagicMock()
        mock_im.get_identity_prompt_block.return_value = "═══ IDENTIDAD ═══\nNombre: María"

        agent = _make_agent()

        with patch(
            "hermes_cli.plugins.get_plugin_context_engine",
            return_value=mock_engine,
        ), patch(
            "agent.identity_manager.get_identity_manager",
            return_value=mock_im,
        ), patch(
            "agent.conversation_index.get_conversation_index",
            return_value=MagicMock(get_recent_conversations=MagicMock(return_value=[])),
        ):
            from agent.system_prompt import build_system_prompt_parts
            parts = build_system_prompt_parts(agent)

        assert "═══ IDENTIDAD ═══" in parts["volatile"]


# -- 4. Falls back when engine raises exception -----------------------------

class TestFallbackOnException:
    """When the plugin system or engine raises, manual blocks are used."""

    def test_import_error_triggers_fallback(self):
        mock_im = MagicMock()
        mock_im.get_identity_prompt_block.return_value = "═══ IDENTIDAD ═══\nNombre: María"

        agent = _make_agent()

        with patch(
            "hermes_cli.plugins.get_plugin_context_engine",
            side_effect=ImportError("plugin not loaded"),
        ), patch(
            "agent.identity_manager.get_identity_manager",
            return_value=mock_im,
        ), patch(
            "agent.conversation_index.get_conversation_index",
            return_value=MagicMock(get_recent_conversations=MagicMock(return_value=[])),
        ):
            from agent.system_prompt import build_system_prompt_parts
            parts = build_system_prompt_parts(agent)

        assert "═══ IDENTIDAD ═══" in parts["volatile"]

    def test_runtime_error_in_engine_triggers_fallback(self):
        mock_engine = MagicMock()
        mock_engine.name = "temporal"
        mock_engine.build_progressive.side_effect = RuntimeError("DB locked")

        mock_im = MagicMock()
        mock_im.get_identity_prompt_block.return_value = "═══ IDENTIDAD ═══\nNombre: María"

        agent = _make_agent()

        with patch(
            "hermes_cli.plugins.get_plugin_context_engine",
            return_value=mock_engine,
        ), patch(
            "agent.identity_manager.get_identity_manager",
            return_value=mock_im,
        ), patch(
            "agent.conversation_index.get_conversation_index",
            return_value=MagicMock(get_recent_conversations=MagicMock(return_value=[])),
        ):
            from agent.system_prompt import build_system_prompt_parts
            parts = build_system_prompt_parts(agent)

        assert "═══ IDENTIDAD ═══" in parts["volatile"]


# -- 5. Falls back when engine name is not "temporal" -----------------------

class TestFallbackWhenNotTemporal:
    """If a different context engine is registered, don't use it for temporal blocks."""

    def test_non_temporal_engine_ignored(self):
        mock_engine = MagicMock()
        mock_engine.name = "compressor"

        mock_im = MagicMock()
        mock_im.get_identity_prompt_block.return_value = "═══ IDENTIDAD ═══\nNombre: María"

        agent = _make_agent()

        with patch(
            "hermes_cli.plugins.get_plugin_context_engine",
            return_value=mock_engine,
        ), patch(
            "agent.identity_manager.get_identity_manager",
            return_value=mock_im,
        ), patch(
            "agent.conversation_index.get_conversation_index",
            return_value=MagicMock(get_recent_conversations=MagicMock(return_value=[])),
        ):
            from agent.system_prompt import build_system_prompt_parts
            parts = build_system_prompt_parts(agent)

        # Manual identity block used, not the compressor engine
        assert "═══ IDENTIDAD ═══" in parts["volatile"]
        mock_engine.build_progressive.assert_not_called()


# -- 6. Memory store still present with temporal engine ----------------------

class TestMemoryStoreWithTemporal:
    """Memory store and user profile sections are independent of temporal engine."""

    def test_memory_block_present_with_temporal(self):
        mock_engine = MagicMock()
        mock_engine.name = "temporal"
        mock_engine.build_progressive.return_value = "temporal context"

        mock_store = MagicMock()
        mock_store.format_for_system_prompt.side_effect = lambda t: (
            "MEMORY BLOCK" if t == "memory" else None
        )

        agent = _make_agent(
            _memory_store=mock_store,
            _memory_enabled=True,
            _user_profile_enabled=False,
        )

        with patch(
            "hermes_cli.plugins.get_plugin_context_engine",
            return_value=mock_engine,
        ):
            from agent.system_prompt import build_system_prompt_parts
            parts = build_system_prompt_parts(agent)

        assert "MEMORY BLOCK" in parts["volatile"]
        assert "temporal context" in parts["volatile"]


# -- 7. External memory provider still works ---------------------------------

class TestExternalMemoryWithTemporal:
    """External memory provider block is independent of temporal engine."""

    def test_external_memory_present_with_temporal(self):
        mock_engine = MagicMock()
        mock_engine.name = "temporal"
        mock_engine.build_progressive.return_value = "temporal context"

        mock_manager = MagicMock()
        mock_manager.build_system_prompt.return_value = "EXTERNAL MEMORY"

        agent = _make_agent(_memory_manager=mock_manager)

        with patch(
            "hermes_cli.plugins.get_plugin_context_engine",
            return_value=mock_engine,
        ):
            from agent.system_prompt import build_system_prompt_parts
            parts = build_system_prompt_parts(agent)

        assert "EXTERNAL MEMORY" in parts["volatile"]
        assert "temporal context" in parts["volatile"]


# -- 8. Timestamp always present --------------------------------------------

class TestTimestampAlwaysPresent:
    """Timestamp line is always in volatile, regardless of engine."""

    def test_timestamp_with_temporal_engine(self):
        mock_engine = MagicMock()
        mock_engine.name = "temporal"
        mock_engine.build_progressive.return_value = "temporal context"

        agent = _make_agent()

        with patch(
            "hermes_cli.plugins.get_plugin_context_engine",
            return_value=mock_engine,
        ):
            from agent.system_prompt import build_system_prompt_parts
            parts = build_system_prompt_parts(agent)

        assert "Conversation started:" in parts["volatile"]

    def test_timestamp_without_temporal_engine(self):
        agent = _make_agent()

        with patch(
            "hermes_cli.plugins.get_plugin_context_engine",
            return_value=None,
        ), patch(
            "agent.identity_manager.get_identity_manager",
            side_effect=Exception("no identity"),
        ), patch(
            "agent.conversation_index.get_conversation_index",
            return_value=MagicMock(get_recent_conversations=MagicMock(return_value=[])),
        ):
            from agent.system_prompt import build_system_prompt_parts
            parts = build_system_prompt_parts(agent)

        assert "Conversation started:" in parts["volatile"]


# -- 9. Manual fallback blocks NOT added when temporal engine succeeds -------

class TestNoDoubleBlocks:
    """When temporal engine succeeds, manual identity+conversation blocks are skipped."""

    def test_no_manual_identity_with_temporal(self):
        mock_engine = MagicMock()
        mock_engine.name = "temporal"
        mock_engine.build_progressive.return_value = "═══ IDENTIDAD ═══\nNombre: Test"

        agent = _make_agent()

        with patch(
            "hermes_cli.plugins.get_plugin_context_engine",
            return_value=mock_engine,
        ), patch(
            "agent.identity_manager.get_identity_manager",
        ) as mock_im_getter, patch(
            "agent.conversation_index.get_conversation_index",
        ) as mock_ci_getter:
            from agent.system_prompt import build_system_prompt_parts
            build_system_prompt_parts(agent)

        # These should NOT have been called since temporal engine succeeded
        mock_im_getter.assert_not_called()
        mock_ci_getter.assert_not_called()


# -- 10. build_system_prompt full integration --------------------------------

class TestBuildSystemPromptFull:
    """The full build_system_prompt() function works with temporal engine."""

    def test_full_prompt_with_temporal(self):
        mock_engine = MagicMock()
        mock_engine.name = "temporal"
        mock_engine.build_progressive.return_value = (
            "═══ IDENTIDAD ═══\nNombre: María\n\n"
            "═══ ÚLTIMOS 14 DÍAS ═══\n  • Día 47: Test session"
        )

        agent = _make_agent()

        with patch(
            "hermes_cli.plugins.get_plugin_context_engine",
            return_value=mock_engine,
        ):
            from agent.system_prompt import build_system_prompt
            result = build_system_prompt(agent)

        assert "═══ IDENTIDAD ═══" in result
        assert "═══ ÚLTIMOS 14 DÍAS ═══" in result
        assert "Conversation started:" in result
