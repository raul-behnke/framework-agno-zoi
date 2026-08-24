"""SignalRateLimitRule — 1 signal per turn + signal name must be declared in node."""

from __future__ import annotations

from typing import Any

from zoi_agno.contracts import Command
from zoi_agno.enforcement.rule import Rejection


class SignalRateLimitRule:
    name = "signal_rate"

    async def check(
        self,
        cmd: Command,
        state: dict[str, Any],
        ctx: dict[str, Any],
    ) -> Rejection | None:
        if cmd.kind != "signal":
            return None
        if int(ctx.get("signals_emitted_this_turn", 0)) >= 1:
            return Rejection(
                self.name, "signal_rate_limited", cmd.kind, "at most 1 signal per turn"
            )
        declared = set(ctx.get("node_signals", []) or [])
        if cmd.payload.name not in declared:
            return Rejection(
                self.name,
                "signal_unknown_in_node",
                cmd.kind,
                f"signal {cmd.payload.name!r} not declared in node",
                extra={"declared": sorted(declared)},
            )
        return None
