"""FinishFlowGraphRule — reject premature ``finish_flow`` short-circuits.

UAT 2026-05-26 sid ``36d553aeadfb`` (imob_sdr post-routine-cutover):
after the last Collect (``c_decisor``) cmd_gen LLM emitted
``finish_flow {outcome: completed}`` directly, skipping every
intermediate Tool / FreeTalk / CallSubroutine node downstream
(``tool_search_imoveis`` → ``ft_apresenta`` → ``ca_agendamento``).
Zero rejections — cmd_gen never even attempted ``start_subflow``.

Root cause (V4 architectural): cmd_gen sees only ``current_node`` +
``plan_state`` + ``state_summary``. It does not consume the full graph,
so once every collect slot is populated it reasons "qualification
done" → emits ``finish_flow``. PlanExecutor would normally advance
through Tool/FreeTalk via ``next:`` but cmd_gen jumps the rail.

This rule enforces: ``finish_flow`` is allowed **only when
``current_node`` IS an EndNode**. Anywhere else it is rejected so
cmd_gen must emit ``skip_collect`` / ``start_subflow`` / ``signal`` /
``set_slot`` to advance the plan first, eventually reaching an
EndNode through the graph.

Research: ``superpowers/research/2026-05-22-v4-bulk-extract-no-fast-forward.md``
Plan: ``superpowers/plans/2026-05-22-routine-cutover-plan.md`` (Path B
follow-up).
"""

from __future__ import annotations

from typing import Any

from zoi_agno.contracts import Command
from zoi_agno.enforcement.rule import Rejection


class FinishFlowGraphRule:
    """Reject ``finish_flow`` unless ``current_node`` is an EndNode."""

    name = "finish_flow_graph"

    async def check(
        self,
        cmd: Command,
        state: dict[str, Any],
        ctx: dict[str, Any],
    ) -> Rejection | None:
        if cmd.kind != "finish_flow":
            return None
        node_def = ctx.get("current_node_def") or {}
        # Accept finish_flow only at an EndNode. The runtime context exposes
        # the node's discriminator under both ``kind`` (routine path) and
        # ``type`` (legacy alias). Tolerate either to keep the rule
        # adapter-agnostic post-routine cutover.
        node_kind = (node_def.get("kind") or node_def.get("type") or "").lower()
        if node_kind in {"end", "endnode"}:
            return None
        current_node_id = state.get("current_node", "") or ""
        return Rejection(
            self.name,
            "finish_flow_premature",
            cmd.kind,
            (
                f"finish_flow not allowed on node {current_node_id!r} of kind "
                f"{node_kind!r}; emit skip_collect/start_subflow/signal to "
                "advance the plan toward an EndNode first."
            ),
            extra={"current_node": current_node_id, "current_node_kind": node_kind},
            # ``finish_flow`` lives in dispatcher ``_NEVER_KINDS`` (universal
            # escape hatch). Force this specific graph-position rejection to
            # SOFT so the command is actually dropped instead of accepted
            # despite the rejection. Other rejections of finish_flow (e.g.
            # PermissionRule) still defer to the kind-based "never" policy.
            force_decision="soft",
        )


__all__ = ["FinishFlowGraphRule"]
