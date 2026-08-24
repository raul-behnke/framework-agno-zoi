"""CostCapRule v4 — turn and root budget caps (decimal USD)."""

from __future__ import annotations

from typing import Any

from zoi_agno.contracts import Command
from zoi_agno.enforcement.rule import Rejection


class CostCapRule:
    name = "cost_cap_v4"

    def __init__(self, max_usd_per_turn: float = 0.05, max_budget_usd: float = 10.0) -> None:
        self.turn_cap = float(max_usd_per_turn)
        self.budget_cap = float(max_budget_usd)

    async def check(
        self,
        cmd: Command,
        state: dict[str, Any],
        ctx: dict[str, Any],
    ) -> Rejection | None:
        turn_usd = float(ctx.get("turn_usd", 0.0))
        root_usd = float(ctx.get("root_conversation_usd", 0.0))
        if turn_usd > self.turn_cap:
            return Rejection(
                self.name,
                "turn_budget_exceeded",
                cmd.kind,
                f"turn ${turn_usd:.4f} > cap ${self.turn_cap}",
            )
        if root_usd > self.budget_cap:
            return Rejection(
                self.name,
                "root_budget_exceeded",
                cmd.kind,
                f"root ${root_usd:.4f} > cap ${self.budget_cap}",
            )
        return None
