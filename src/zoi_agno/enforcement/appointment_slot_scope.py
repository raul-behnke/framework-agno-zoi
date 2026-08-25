"""Horário escolhido tem que existir na agenda oferecida.

``set_slot`` no slot de horário só passa se o valor for um ``slot_id`` que a
agenda devolveu. LLM inventou horário → rejeição soft, e o extrator tenta de
novo. Sem agenda no estado → no-op, porque a conversa nem chegou lá.

**Os nomes são configuráveis por tenant.** No v4 eles eram fixos
(``slot_escolhido`` / ``available_slots``), o que faz a rule ficar INERTE num
roteiro que chame os campos de outra coisa — e uma proteção que depende de o
autor adivinhar o nome não protege vertical nova. Descoberto na Barbearia,
onde os campos são ``horario`` e ``agenda``: a rule existia e não checava
nada.

Configuração, em ``business.yaml``::

    agendamento:
      slot_de_horario: horario     # default: slot_escolhido
      payload_da_agenda: agenda    # default: available_slots
"""

from __future__ import annotations

from typing import Any

from zoi_agno.enforcement.rule import Rejection

_SLOT_PADRAO = "slot_escolhido"
_PAYLOAD_PADRAO = "available_slots"


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
        cfg = (ctx.get("business") or {}).get("agendamento") or {}
        alvo = str(cfg.get("slot_de_horario") or _SLOT_PADRAO)
        chave_agenda = str(cfg.get("payload_da_agenda") or _PAYLOAD_PADRAO)

        if str(_payload_get(payload, "slot") or "") != alvo:
            return None
        av = (state.get("collected") or {}).get(chave_agenda) or {}
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
