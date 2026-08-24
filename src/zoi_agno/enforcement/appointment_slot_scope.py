"""AppointmentSlotScopeRule — anti-fabricação de slot (decisão #8a do spec scheduling).

Espelho do C1 (ID containment) na camada de commands: ``set_slot slot_escolhido``
só é aceito se o valor ∈ slot_ids oferecidos por get_available_slots
(state.collected.available_slots). LLM inventou horário → soft reject, cmdgen
re-tenta/clarify. Sem available_slots no state → no-op (fora do fluxo).
"""

from __future__ import annotations

from typing import Any

from zoi_agno.enforcement.rule import Rejection

_TARGET_SLOT = "slot_escolhido"


def _payload_get(payload: Any, key: str) -> Any:
    """Handle both dict payload (test stubs) and Pydantic attribute payload (real commands)."""
    if isinstance(payload, dict):
        return payload.get(key)
    return getattr(payload, key, None)


class AppointmentSlotScopeRule:
    name = "appointment_slot_scope"

    async def check(
        self,
        cmd: Any,
        state: dict[str, Any],
        ctx: dict[str, Any],
    ) -> Rejection | None:
        if getattr(cmd, "kind", None) != "set_slot":
            return None
        payload = getattr(cmd, "payload", None)
        if str(_payload_get(payload, "slot") or "") != _TARGET_SLOT:
            return None
        av = (state.get("collected") or {}).get("available_slots") or {}
        offered = {
            str(s.get("slot_id"))
            for s in (av.get("slots") or [])
            if isinstance(s, dict) and s.get("slot_id")
        }
        if not offered:
            return None
        value = str(_payload_get(payload, "value") or "")
        if value in offered:
            return None
        return Rejection(
            rule=self.name,
            code="slot_not_in_offer",
            command_kind=cmd.kind,
            detail=(
                f"slot_escolhido {value[:48]!r} não está entre os horários oferecidos — "
                "use exatamente um slot_id de available_slots"
            ),
            extra={"offered": sorted(offered)},
            command=cmd,
        )
