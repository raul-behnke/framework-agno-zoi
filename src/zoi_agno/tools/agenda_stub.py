"""Agenda determinística — substitui o calendário do CRM nos fixtures.

Devolve horários estáveis derivados de uma data-base, para que a mesma
conversa produza a mesma agenda em toda execução. Sem isso, um golden que
menciona horário quebraria a cada dia.

O contrato de saída espelha o que o runtime espera de um calendário real:
``{"slots": [{"slot_id", "inicio", "label"}], "total": N}``. A rule
``appointment_slot_scope`` valida a escolha do lead contra ``slot_id``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

# Fixa de propósito: agenda reproduzível entre execuções e entre máquinas.
BASE = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
HORARIOS = (0, 2, 5, 24, 26, 48)  # horas a partir da base
_DIAS = ("segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo")


def agenda_livre(
    servico: str | None = None,
    profissional: str | None = None,
    max_results: int = 4,
    **_: Any,
) -> dict[str, Any]:
    """Horários livres para um serviço.

    ``servico`` e ``profissional`` entram no ``slot_id`` para que agendas de
    serviços diferentes não colidam — o suficiente para exercitar o fluxo.
    """
    marca = (servico or "geral").strip().lower().replace(" ", "-")
    slots: list[dict[str, Any]] = []
    for h in HORARIOS[: max(1, min(max_results, len(HORARIOS)))]:
        inicio = BASE + timedelta(hours=h)
        slots.append(
            {
                "slot_id": f"{marca}:{inicio:%Y-%m-%dT%H:%M}",
                "inicio": inicio.isoformat(),
                "label": f"{_DIAS[inicio.weekday()]} às {inicio:%H:%M}",
            }
        )
    return {"slots": slots, "total": len(slots), "servico": servico, "profissional": profissional}
