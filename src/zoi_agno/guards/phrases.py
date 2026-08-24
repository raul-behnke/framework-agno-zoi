"""Frases proibidas da persona — o backstop determinístico.

O `persona.yaml` declara padrões que o agente nunca deve escrever: revelar
que é IA, bordões que ninguém fala ("rapidinho"), promessas que viram
problema jurídico ("aprovação garantida"). Alguns são literais, outros regex.

Isto roda depois da geração porque o modelo desobedece o prompt sob pressão —
e é justamente sob pressão (lead insistente) que a frase proibida escapa.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from zoi_agno.guards.fabrication import Violacao


def _dobrar(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", str(s))
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


def checar_frases_proibidas(texto: str, persona: dict[str, Any]) -> list[Violacao]:
    """Padrões da persona encontrados no texto.

    ``mode: ban`` é o único que gera violação; qualquer outro modo é aviso e
    fica a cargo do crítico de tom.
    """
    if not texto:
        return []
    dobrado = _dobrar(texto)
    achados: list[Violacao] = []
    for regra in persona.get("forbidden_phrases") or []:
        if not isinstance(regra, dict) or regra.get("mode") != "ban":
            continue
        padrao = regra.get("pattern")
        if not padrao:
            continue
        try:
            if regra.get("is_regex"):
                m = re.search(_dobrar(padrao), dobrado)
                if m:
                    achados.append(Violacao("frase_proibida", str(padrao), m.group(0)))
            elif _dobrar(padrao) in dobrado:
                achados.append(Violacao("frase_proibida", str(padrao), str(padrao)))
        except re.error:
            continue  # padrão malformado do autor não derruba o turno
    return achados
