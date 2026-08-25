"""Superlativos de linguagem natural → campo e direção de ordenação.

"a mais potente" vira ordenação decrescente por ``potencia_w``; "a mais
barata", crescente por ``valor_brl``. Portado de ``candidate_pick.py`` do v4,
onde o comentário registra a caça: por muito tempo só preço tinha superlativo,
e "a mais potente" simplesmente não resolvia.

Os nomes de campo são convenção de catálogo, não do runtime — um catálogo que
não tenha ``potencia_w`` só não casa com esse padrão.
"""

from __future__ import annotations

import re

# (padrão, campo, pegar_o_menor)
_SUPERLATIVE: list[tuple[re.Pattern[str], str, bool]] = [
    (re.compile(r"\bmais\s+barat[oa]\b", re.IGNORECASE), "valor_brl", True),
    (re.compile(r"\bmais\s+(?:economic[oa]|em\s+conta)\b", re.IGNORECASE), "valor_brl", True),
    (re.compile(r"\bmais\s+car[oa]\b", re.IGNORECASE), "valor_brl", False),
    (re.compile(r"\bmais\s+(?:potente|forte|possante)\b", re.IGNORECASE), "potencia_w", False),
    (re.compile(r"\bmais\s+(?:autonomia|alcance)\b", re.IGNORECASE), "autonomia_km", False),
    (
        re.compile(r"\b(maior|mais\s+amplo|mais\s+espa[çc]os[oa])\b", re.IGNORECASE),
        "area_m2",
        False,
    ),
    (re.compile(r"\b(menor|mais\s+compact[oa])\b", re.IGNORECASE), "area_m2", True),
]

__all__ = ["_SUPERLATIVE"]
