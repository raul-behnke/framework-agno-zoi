"""PlanReachabilityRule — validates replan target nodes vs reachable graph."""

from __future__ import annotations

from typing import Any

from zoi_agno.contracts import Command
from zoi_agno.enforcement.rule import Rejection


class PlanReachabilityRule:
    name = "plan_reach"

    def __init__(self, max_steps: int = 12) -> None:
        self.max_steps = max_steps

    async def check(
        self,
        cmd: Command,
        state: dict[str, Any],
        ctx: dict[str, Any],
    ) -> Rejection | None:
        if cmd.kind != "replan":
            return None

        steps = cmd.payload.new_plan
        if len(steps) > self.max_steps:
            return Rejection(
                self.name,
                "plan_too_long",
                cmd.kind,
                f"plan has {len(steps)} steps, max {self.max_steps}",
            )

        reachable = set(ctx.get("reachable_nodes", []) or [])
        for step in steps:
            tgt = getattr(step, "target", None) or (
                step.get("target") if isinstance(step, dict) else None
            )
            if tgt not in reachable:
                return Rejection(
                    self.name,
                    "plan_unreachable",
                    cmd.kind,
                    f"step target {tgt!r} not reachable",
                    extra={"target": tgt},
                )
        return None
