"""CollectGroupExitRule — enforces exit_policy + max_turns on collect_group nodes."""

from __future__ import annotations

from typing import Any

from zoi_agno.contracts import Command
from zoi_agno.enforcement.rule import Rejection


class CollectGroupExitRule:
    name = "collect_group_exit"

    async def check(
        self,
        cmd: Command,
        state: dict[str, Any],
        ctx: dict[str, Any],
    ) -> Rejection | None:
        node = ctx.get("current_node_def") or {}
        if node.get("kind") != "collect_group":
            return None

        max_turns = int(node.get("max_turns", 10))
        turns = int(state.get("turns_in_node", 0))
        if turns >= max_turns:
            return Rejection(
                rule=self.name,
                code="collect_group_max_turns_exceeded",
                command_kind=cmd.kind,
                detail=f"node {node.get('id')!r} max_turns={max_turns} exceeded",
            )

        if not ctx.get("is_advance_intent"):
            return None

        policy = node.get("exit_policy", "all_required")
        fields = node.get("fields", []) or []
        required = [f["name"] for f in fields if f.get("required")]
        collected = set(state.get("collected", {}) or {})

        if policy == "all_required":
            missing = [s for s in required if s not in collected]
            if missing:
                return Rejection(
                    rule=self.name,
                    code="collect_group_required_missing",
                    command_kind=cmd.kind,
                    detail=f"missing required slots: {missing}",
                    extra={"missing": missing},
                )
        elif policy == "any_n":
            min_req = int(node.get("min_required") or 0)
            satisfied = sum(1 for s in required if s in collected)
            if satisfied < min_req:
                return Rejection(
                    rule=self.name,
                    code="collect_group_required_missing",
                    command_kind=cmd.kind,
                    detail=f"any_n needs {min_req}, got {satisfied}",
                )
        elif policy == "all_declared":
            all_names = [f["name"] for f in fields]
            missing = [s for s in all_names if s not in collected]
            if missing:
                return Rejection(
                    rule=self.name,
                    code="collect_group_required_missing",
                    command_kind=cmd.kind,
                    detail=f"all_declared needs all, missing: {missing}",
                )

        return None
