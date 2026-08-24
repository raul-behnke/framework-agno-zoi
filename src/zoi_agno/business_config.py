"""Configuração de tratamento de objeções, lida de ``business.yaml``.

Portado de ``zoi_agent/persona/business.py``. Só o bloco de objeções — o
restante do modelo de business entra na Fase 4, quando os cérebros
passarem a consumi-lo.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

CANONICAL_OBJECTIONS: tuple[str, ...] = (
    "preco",
    "vou_pensar",
    "terceiro_decisor",
    "concorrente",
    "desconfianca",
    "sem_tempo",
)


class ObjectionTypeConfig(BaseModel):
    """Override por-tipo dentro de `ObjectionsConfig.types` (em business.yaml::objections.types)."""

    fallback: Literal["nurture", "handoff", "desqualificar"] = "nurture"
    max_attempts: int | None = None
    end_id: str | None = None
    description: str = ""


class ObjectionsConfig(BaseModel):
    """Per-tenant opt-in pra objection handling (em business.yaml::objections).

    Default OFF. Tipos canônicos (`CANONICAL_OBJECTIONS`) resolvem pro
    fallback `nurture` com `max_attempts` global se não declarados em
    `types`; tenants podem declarar tipos extras (ex.: `golpe`) ou override
    por-tipo. `fallback=desqualificar` exige `end_id` — fail-loud senão.
    """

    enabled: bool = False
    max_attempts: int = 2
    types: dict[str, ObjectionTypeConfig] = Field(default_factory=dict)
    # Fix D2 (2026-07-20) — detecção DETERMINÍSTICA via placar da routine: mapa
    # `signal name → tipo de objeção canônico` (ex.: {caca_preco: preco}). Quando
    # o cmdgen omite o campo opcional `objection` (visto live: gpt-5.4-mini
    # prefere o sinal que os few-shots ensinam), o call site do bump deriva o
    # tipo do primeiro comando `signal` presente no mapa. Default {} = zero
    # mudança. NÃO mapear gatilhos fortes (ma_fe) — troll continua cortando.
    signal_map: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_desqualificar_has_end_id(self) -> ObjectionsConfig:
        for tid, t in self.types.items():
            if t.fallback == "desqualificar" and not t.end_id:
                raise ValueError(
                    f"objections.types[{tid!r}].fallback=desqualificar requires end_id"
                )
        for sig, tid in self.signal_map.items():
            if tid not in CANONICAL_OBJECTIONS and tid not in self.types:
                raise ValueError(
                    f"objections.signal_map[{sig!r}]={tid!r} não é tipo canônico nem declarado em types"
                )
        return self

    def resolve(self, type_id: str) -> tuple[str, int] | None:
        if type_id not in CANONICAL_OBJECTIONS and type_id not in self.types:
            return None
        t = self.types.get(type_id)
        fallback = t.fallback if t else "nurture"
        max_att = t.max_attempts if t and t.max_attempts is not None else self.max_attempts
        return (fallback, max_att)

    def known_types(self) -> list[tuple[str, str]]:
        out: dict[str, str] = dict.fromkeys(CANONICAL_OBJECTIONS, "")
        for tid, t in self.types.items():
            out[tid] = t.description
        return list(out.items())
