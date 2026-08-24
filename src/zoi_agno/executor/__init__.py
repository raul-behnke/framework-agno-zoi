"""O executor determinístico — move o cursor do grafo, sem LLM."""

from __future__ import annotations

from zoi_agno.executor.advance import (
    AdvanceResult,
    WaitNotImplementedError,
    advance,
    collect_group_satisfied,
    current_block,
    current_node,
    missing_required,
    route_decide,
)
from zoi_agno.executor.values import MISSING, is_filled, normalize_value, resolve_path

__all__ = [
    "MISSING",
    "AdvanceResult",
    "WaitNotImplementedError",
    "advance",
    "collect_group_satisfied",
    "current_block",
    "current_node",
    "is_filled",
    "missing_required",
    "normalize_value",
    "resolve_path",
    "route_decide",
]
