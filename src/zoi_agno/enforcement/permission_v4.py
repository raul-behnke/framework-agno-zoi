"""PermissionRule v4 — role-based command kind allowlist.

`handoff_human` and `finish_flow` are universal escape hatches and bypass
role restrictions.
"""

from __future__ import annotations

from typing import Any

from zoi_agno.contracts import Command
from zoi_agno.enforcement.rule import Rejection

_ALWAYS_ALLOWED = {"handoff_human", "finish_flow"}


# FIND-029 — explicit default allowlist for the "agent" role. Tenants
# wanting stricter permissioning override via runtime_context.allowed_kinds.
DEFAULT_AGENT_ALLOWED_KINDS: dict[str, set[str]] = {
    "agent": {
        "set_slot",
        "confirm_slot",
        "skip_collect",
        "start_subflow",
        "cancel_subflow",
        "clarify",
        "replan",
        "say_freetalk",
        "signal",
        "record_fact",
        "annotate_interaction",
        "handoff_human",
        "finish_flow",
        "send_album",  # Task 4.2 — on-demand album request
    },
}


class PermissionRule:
    name = "permission_v4"

    def __init__(self, allowed_kinds: dict[str, set[str]] | None = None) -> None:
        self.allowed = allowed_kinds or {}

    async def check(
        self,
        cmd: Command,
        state: dict[str, Any],
        ctx: dict[str, Any],
    ) -> Rejection | None:
        if cmd.kind in _ALWAYS_ALLOWED:
            return None
        role = ctx.get("role")
        if not role or role not in self.allowed:
            return None
        if cmd.kind not in self.allowed[role]:
            return Rejection(
                self.name,
                "command_not_allowed_for_role",
                cmd.kind,
                f"role {role!r} cannot emit {cmd.kind!r}",
                extra={"role": role, "allowed": sorted(self.allowed[role])},
            )
        return None
