"""Named coercer library for the generic catalog_search motor.

Pure, tolerant parsers extracted from the bespoke domain tools
(`property.py:44-108` + `vehicle.py:30-86`) and registered by name in
:data:`COERCERS`. Config references a coercer by its registry key via
``coerce: <name>``. A field with no ``coerce`` declared uses identity.

The money parser is the *merged* version: it keeps the property.py base
behaviour AND the vehicle.py "entre X e Y" range handling (takes the upper
bound). This keeps a single canonical coercer for both tenants.

Closed set — adding an exotic coercer is a Python edit (+1 entry here), never
a config-author concern. Unknown coercer names fail loud at spec build time
(Phase 6), not per-call.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

_NUM_WORDS_PT: dict[str, int] = {
    "zero": 0,
    "um": 1,
    "uma": 1,
    "dois": 2,
    "duas": 2,
    "tres": 3,
    "três": 3,
    "quatro": 4,
    "cinco": 5,
    "seis": 6,
    "sete": 7,
    "oito": 8,
    "nove": 9,
    "dez": 10,
}

_TRUE_WORDS_PT: frozenset[str] = frozenset(
    {"sim", "s", "true", "verdadeiro", "yes", "y", "1", "ok", "claro", "positivo"}
)
_FALSE_WORDS_PT: frozenset[str] = frozenset(
    {"nao", "não", "n", "false", "falso", "no", "0", "nunca", "negativo"}
)


def parse_money_brl(value: Any) -> float | None:
    """Parse texto livre de valor BRL para float.

    Aceita: 800000, "800 mil", "R$ 800.000,00", "800k", "1,2 mi",
    "1.2 milhões", "até 90 mil", "entre 60 e 80" (pega o teto superior do
    range). Retorna ``None`` se falhar.
    """
    if value is None or value == "" or value == "null":
        return None
    if isinstance(value, int | float):
        return float(value)
    s = str(value).lower().strip()
    if not s:
        return None
    # Em "entre X e Y" pegar Y (teto superior) — de vehicle.py:42-44.
    m = re.match(r".*?entre\s+\d[\d.,]*\s+e\s+(\d[\d.,]*).*", s)
    if m:
        s = m.group(1)
    s = s.replace("r$", "").replace("reais", "").replace("brl", "").strip()
    mult = 1.0
    if re.search(r"\bmilh|\bmi\b|\bmm\b", s):
        mult = 1_000_000.0
    elif re.search(r"\bmil\b|\bk\b", s):
        mult = 1_000.0
    s = re.sub(r"\b(milh[õo]es?|milhao|milhão|mil|mi|mm|k)\b", "", s).strip()
    s = re.sub(r"\b(uns?|umas?|ate|até|por\s+volta\s+de|aproximadamente)\b", "", s).strip()
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    else:
        if re.fullmatch(r"\d{1,3}(\.\d{3})+", s):
            s = s.replace(".", "")
    s = re.sub(r"[^\d.]", "", s)
    if not s:
        return None
    try:
        return float(s) * mult
    except ValueError:
        return None


def parse_int_count(value: Any) -> int | None:
    """Parse '2', '2 quartos', 'dois', 'três'. ``None`` se falhar."""
    if value is None or value == "" or value == "null":
        return None
    if isinstance(value, bool):
        # bool é subclasse de int — não tratar True/False como contagem.
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    s = str(value).lower().strip()
    if not s:
        return None
    for word, n in _NUM_WORDS_PT.items():
        if re.search(rf"\b{word}\b", s):
            return n
    m = re.search(r"\d+", s)
    if m:
        try:
            return int(m.group(0))
        except ValueError:
            return None
    return None


def parse_year(value: Any) -> int | None:
    """Parse '2020', '2020 pra cima', 'a partir de 2018'. ``None`` se falhar."""
    if value is None or value == "" or value == "null":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    s = str(value).lower().strip()
    if not s:
        return None
    m = re.search(r"\b(19|20)\d{2}\b", s)
    if m:
        try:
            return int(m.group(0))
        except ValueError:
            return None
    return None


def split_token_list(value: Any) -> list[str]:
    """Aceita lista OU texto livre separado por ,/;/e/+. Lower + trim.

    De `property.py:101-108` (`_split_caracteristicas`).
    """
    if value is None or value == "" or value == "null":
        return []
    if isinstance(value, list):
        return [str(x).lower().strip() for x in value if str(x).strip()]
    parts = re.split(r"[,;/\n]|\s+e\s+|\s+\+\s+", str(value).lower())
    return [p.strip() for p in parts if p.strip() and len(p.strip()) > 1]


def parse_bool_pt(value: Any) -> bool | None:
    """Parse 'sim'/'não' (e variações pt/en) para bool. ``None`` se ambíguo."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return bool(value)
    s = str(value).lower().strip()
    if not s or s == "null":
        return None
    if s in _TRUE_WORDS_PT:
        return True
    if s in _FALSE_WORDS_PT:
        return False
    return None


COERCERS: dict[str, Callable[[Any], Any]] = {
    "money_brl": parse_money_brl,
    "int_count": parse_int_count,
    "year": parse_year,
    "token_list": split_token_list,
    "bool_pt": parse_bool_pt,
}


def coerce_value(value: Any, coerce_name: str | None) -> Any:
    """Apply a named coercer. ``None`` name → identity. Unknown name → KeyError.

    Unknown coercer names are a build-time error (Phase 6 fail-loud); callers
    that want fail-loud should validate against :data:`COERCERS` keys up front.
    """
    if coerce_name is None:
        return value
    return COERCERS[coerce_name](value)
