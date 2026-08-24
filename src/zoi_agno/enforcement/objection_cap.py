"""Decisão pura de cap de objeção — zero I/O, zero state mutation.

Consome o contador `_objection_counts` + ObjectionsConfig; devolve o fallback
a aplicar quando um tipo estourou seu teto. O roteamento (mutar current_node /
injetar handoff) é do plan_executor — aqui só a decisão.
"""

from __future__ import annotations

from dataclasses import dataclass

from zoi_agno.business_config import ObjectionsConfig


@dataclass(frozen=True)
class ObjectionCapDecision:
    type_id: str
    fallback: str
    end_id: str | None


def objection_cap_decision(
    counts: dict[str, int], config: ObjectionsConfig
) -> ObjectionCapDecision | None:
    for type_id, count in counts.items():
        resolved = config.resolve(type_id)
        if resolved is None:
            continue
        fallback, max_attempts = resolved
        if count > max_attempts:
            end_id = None
            t = config.types.get(type_id)
            if t is not None:
                end_id = t.end_id
            return ObjectionCapDecision(type_id=type_id, fallback=fallback, end_id=end_id)
    return None
