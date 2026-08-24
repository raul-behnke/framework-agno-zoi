"""SubflowRule v4 — adapts legacy subflow guard to command bus shape."""

from __future__ import annotations

from typing import Any

from zoi_agno.contracts import Command
from zoi_agno.enforcement.rule import Rejection


class SubflowRule:
    name = "subflow_v4"

    def __init__(self, max_depth: int = 3) -> None:
        self.max_depth = max_depth

    async def check(
        self,
        cmd: Command,
        state: dict[str, Any],
        ctx: dict[str, Any],
    ) -> Rejection | None:
        if cmd.kind == "start_subflow":
            return self._check_start(cmd, state, ctx)
        if cmd.kind == "cancel_subflow":
            return self._check_cancel(cmd, state)
        return None

    def _check_start(
        self, cmd: Command, state: dict[str, Any], ctx: dict[str, Any]
    ) -> Rejection | None:
        ref = cmd.payload.ref
        refs = set(ctx.get("subflow_registry_refs", []) or [])
        if ref not in refs:
            return Rejection(
                self.name, "subflow_unknown_ref", cmd.kind, f"ref {ref!r} not in registry"
            )

        stack = state.get("subflow_stack", []) or []
        if len(stack) >= self.max_depth:
            return Rejection(
                self.name,
                "subflow_depth_exceeded",
                cmd.kind,
                f"depth {len(stack)} >= max {self.max_depth}",
            )

        if any(frame.get("ref") == ref for frame in stack):
            return Rejection(self.name, "subflow_loop", cmd.kind, f"ref {ref!r} already on stack")

        required_inputs = (ctx.get("subflow_required_inputs", {}) or {}).get(ref, [])
        provided = set(cmd.payload.inputs.keys())
        missing = [k for k in required_inputs if k not in provided]
        if missing:
            return Rejection(
                self.name,
                "subflow_missing_inputs",
                cmd.kind,
                f"missing inputs for {ref}: {missing}",
                extra={"missing": missing},
            )
        return None

    def _check_cancel(self, cmd: Command, state: dict[str, Any]) -> Rejection | None:
        if not state.get("subflow_stack"):
            return Rejection(
                self.name, "nothing_to_cancel", cmd.kind, "no active subflow to cancel"
            )
        return None
