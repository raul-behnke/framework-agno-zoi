"""SlotScopeRule — verifies set_slot/confirm_slot targets a slot in the
active flow OR any ancestor subflow on the stack.
"""

from __future__ import annotations

from typing import Any

from zoi_agno.contracts import Command
from zoi_agno.enforcement.rule import Rejection


class SlotScopeRule:
    name = "slot_scope"

    async def check(
        self,
        cmd: Command,
        state: dict[str, Any],
        ctx: dict[str, Any],
    ) -> Rejection | None:
        if cmd.kind not in ("set_slot", "confirm_slot"):
            return None
        slot = cmd.payload.slot
        # FIND-034 — subflow_stack frames have no ``flow_slots`` key in v4;
        # the ancestor merge loop was dead. ctx["flow_slots"] is already the
        # union of active flow + ancestor flows for the active depth (built
        # by ``runtime_context_v4_from_bpmn``).
        flow_wide = set(ctx.get("flow_slots", []))
        if slot in flow_wide:
            return None
        return Rejection(
            rule=self.name,
            code="slot_out_of_scope",
            command_kind=cmd.kind,
            detail=f"slot {slot!r} not in active flow or ancestor subflows",
            extra={"slot": slot, "flow_wide": sorted(flow_wide)},
        )
