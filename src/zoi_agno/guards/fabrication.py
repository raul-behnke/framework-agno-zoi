"""Anti-invenção: código de item citado tem que existir no payload.

Precedente real no v4: com um modelo mais fraco no papel de redator, preço de
veículo saía distorcido em ~1% — perto o bastante para passar despercebido na
revisão, errado o bastante para o lead chegar na loja com a informação errada.

Por isso a checagem é POSTERIOR à geração, não uma instrução no prompt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Códigos de catálogo: duas ou mais letras, hífen, dígitos (AP-001, TM-28).
# Estreito de propósito — pegar "R$ 6.900" aqui daria falso positivo em toda
# frase com número.
_CODIGO_RE = re.compile(r"\b([A-Z]{2,}-\d{1,6})\b")


@dataclass(frozen=True)
class Violacao:
    """Algo no texto que os dados não sustentam."""

    tipo: str
    detalhe: str
    trecho: str = ""


def extrair_codigos(texto: str) -> set[str]:
    return set(_CODIGO_RE.findall(texto or ""))


def _codigos_permitidos(state: dict[str, Any]) -> set[str]:
    """Todo código que apareceu em algum payload de tool desta conversa."""
    permitidos: set[str] = set()
    for valor in (state.get("collected") or {}).values():
        if not isinstance(valor, dict):
            continue
        for cand in valor.get("candidates") or []:
            if isinstance(cand, dict) and cand.get("codigo"):
                permitidos.add(str(cand["codigo"]).upper())
    for cand in state.get("_presented_candidates") or []:
        if isinstance(cand, dict) and cand.get("codigo"):
            permitidos.add(str(cand["codigo"]).upper())
    return permitidos


def checar_grounding(texto: str, state: dict[str, Any]) -> list[Violacao]:
    """Códigos citados que não vieram de nenhum payload.

    Sem nenhum payload na conversa, não há o que comparar — a checagem não
    opina. Assim que existe um payload, todo código citado tem que estar nele.
    """
    citados = extrair_codigos(texto)
    if not citados:
        return []
    permitidos = _codigos_permitidos(state)
    if not permitidos:
        return []
    return [
        Violacao("codigo_inventado", f"{c!r} não está em nenhum payload desta conversa", c)
        for c in sorted(citados)
        if c.upper() not in permitidos
    ]
