"""Helper for appending entries to state.enforcement_rejections (FIND-026).

Four call sites (dispatcher, turn_manager auto-spawn + replan_lock,
plan_executor CG + ToolNode + tool_failed) produced subtly different dict
shapes — some used ``"kind"``, the dispatcher used ``"kind"`` from
``r.command_kind`` (note name mismatch), and ``extra`` was sometimes
missing entirely. This helper normalises to a canonical shape.

Schema (canonical):
    {
        "rule":         str,
        "code":         str,
        "command_kind": str,
        "kind":         str,   # legacy alias preserved for studio + v3 consumers
        "detail":       str,
        "extra":        dict | None,   # optional, omitted when None
    }
"""

from __future__ import annotations

from typing import Any

REJECTION_REQUIRED_KEYS = ("rule", "code", "command_kind", "detail")


def append_rejection(
    state: dict[str, Any],
    *,
    rule: str,
    code: str,
    command_kind: str,
    detail: str,
    extra: dict[str, Any] | None = None,
) -> None:
    """Append a canonical-shape rejection entry to ``state['enforcement_rejections']``.

    The legacy ``"kind"`` key is dual-written alongside ``"command_kind"`` to
    avoid breaking downstream consumers (Studio inspector reads stored JSON).
    """
    entry: dict[str, Any] = {
        "rule": rule,
        "code": code,
        "command_kind": command_kind,
        "kind": command_kind,  # legacy alias — Studio inspector + v3 paths
        "detail": detail,
    }
    if extra is not None:
        entry["extra"] = dict(extra)
    state.setdefault("enforcement_rejections", []).append(entry)
