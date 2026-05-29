"""Temporal Context Engine plugin.

Produces a temporal-aware system prompt with five blocks:
IDENTIDAD, ÚLTIMOS 14 DÍAS, EPISODIO ACTUAL, MEMORIA SEMÁNTICA,
PATRONES DETECTADOS.

Usage:
    from plugins.context_engine.temporal import TemporalContextEngine

    engine = TemporalContextEngine()
    context = engine.build(user_message="hola")

Discovery:
    Set ``context.engine: temporal`` in config.yaml to activate.
"""

from .temporal_context_engine import TemporalContextEngine, create_engine

__all__ = ["TemporalContextEngine", "create_engine"]


def register(ctx) -> None:
    """Plugin registration entry point."""
    from .temporal_context_engine import TemporalContextEngine
    engine = TemporalContextEngine()
    ctx.register_context_engine(engine)
