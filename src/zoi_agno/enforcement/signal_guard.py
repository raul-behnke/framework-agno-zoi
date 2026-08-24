"""SignalGuardRule — sinal guardado só é aceito se a fala do lead o sustenta.

QA zion 2026-07-06 (rodadas r13-r15 da bateria): o cmdgen emite ``recusou``
em qualquer resposta que contenha "não" ("não sei o número certinho", "não,
sou vendedora"), e sinal vence campo no roteamento do Decide — leads válidos
eram mandados pro re-engajamento/desqualificação. Instrução de prompt não
segurou em 3 rodadas.

Espelho do detector determinístico ``lead_requested_human``: um sinal
sensível listado em ``business.guarded_signals`` só é aceito quando a ÚLTIMA
mensagem do lead contém um dos padrões configurados (fold: minúsculas, sem
acento). Sem match → rejeição soft (sinal dropado, conversa segue natural).
Opt-in por tenant; sinais fora do mapa passam direto.
"""

from __future__ import annotations

import unicodedata
from typing import Any

from zoi_agno.contracts import Command
from zoi_agno.enforcement.rule import Rejection


def _fold(s: Any) -> str:
    nfkd = unicodedata.normalize("NFKD", str(s))
    return "".join(c for c in nfkd if not unicodedata.combining(c)).strip().lower()


class SignalGuardRule:
    name = "signal_guard"

    async def check(
        self,
        cmd: Command,
        state: dict[str, Any],
        ctx: dict[str, Any],
    ) -> Rejection | None:
        if cmd.kind != "signal":
            return None
        business = ctx.get("business") or {}
        guarded = business.get("guarded_signals") if isinstance(business, dict) else None
        if not isinstance(guarded, dict) or not guarded:
            return None
        patterns = guarded.get(getattr(cmd.payload, "name", None))
        if not patterns:
            return None
        msg = _fold(state.get("_last_user_msg", "") or "")
        for p in patterns:
            if _fold(p) and _fold(p) in msg:
                return None
        return Rejection(
            rule=self.name,
            code="signal_guard_no_match",
            command_kind=cmd.kind,
            detail=(
                f"signal {cmd.payload.name!r} guarded: last user msg does not "
                f"contain any configured pattern"
            ),
            extra={"signal": cmd.payload.name, "last_user_msg": msg[:120]},
        )
