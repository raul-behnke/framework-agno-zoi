"""BranchGatingSlotRule — um slot que dirige um Decide só pode ser setado
quando o nó ativo é o collect_group que o coleta.

Raiz (root-cause 2026-06-27): o cmdgen alucina `tem_modelo='nao'` na abertura
(nó c_disponivel) → satisfaz o slot required prematuramente → o Decide
`d_tem_modelo` roteia pro branch errado antes do lead ser perguntado. Esta rede
determinística dropa o set_slot de um slot-gating fora do seu nó-dono.

Opt-in por tenant: `business.branch_gating_slots: {<slot>: <no_dono>}`. Mapa
ausente/vazio => no-op (outros tenants intocados).
"""

from __future__ import annotations

from typing import Any

from zoi_agno.contracts import Command
from zoi_agno.enforcement.rule import Rejection


class BranchGatingSlotRule:
    name = "branch_gating_slot"

    async def check(
        self, cmd: Command, state: dict[str, Any], ctx: dict[str, Any]
    ) -> Rejection | None:
        if cmd.kind not in ("set_slot", "confirm_slot"):
            return None
        gating = (ctx.get("business") or {}).get("branch_gating_slots") or {}
        if not isinstance(gating, dict) or not gating:
            return None
        slot = cmd.payload.slot
        owner = gating.get(slot)
        if owner is None:
            return None
        current = state.get("current_node")
        if current == owner:
            return None
        return Rejection(
            rule=self.name,
            code="branch_slot_premature",
            command_kind=cmd.kind,
            detail=f"gating slot {slot!r} set at {current!r}, only allowed at owner {owner!r}",
            extra={"slot": slot, "current_node": current, "owner": owner},
        )
