"""Comparação de valores no roteamento — o que conta como "igual" e como "cheio".

Portado de ``plan_executor.py``. Duas decisões que parecem detalhe e não são:

**Normalização acento-insensível via NFKD**, não um mapa de acentos do
português. O mapa hardcoded quebrava em maiúscula e em qualquer diacrítico
não-português; decompor e descartar as marcas combinantes resolve os três.

**Presença significa valor não-vazio.** Um slot com ``None`` ou ``""`` — que é
o que sobra quando o enforcement rejeita um ``set_slot`` — não conta como
preenchido. Sem isso, um branch por presença deixava passar lead com o campo
vazio e mandava gente qualificada para o re-engajamento.
"""

from __future__ import annotations

import unicodedata
from typing import Any

_TRUTHY = {"true", "1", "yes", "sim", "y", "s", "verdadeiro"}
_FALSY = {"false", "0", "no", "nao", "não", "n", "falso"}


class _Missing:
    """Sentinela: o caminho não existe no estado (≠ existe e está vazio)."""

    def __repr__(self) -> str:  # pragma: no cover — só para depuração
        return "<MISSING>"


MISSING = _Missing()


def normalize_value(v: Any) -> str:
    """Forma canônica para comparar ``on_value`` com o que o lead disse.

    Booleano vira ``sim``/``nao``; acento e caixa são descartados; as grafias
    de verdadeiro/falso convergem.
    """
    if v is None:
        return ""
    if isinstance(v, bool):
        return "sim" if v else "nao"
    s = str(v).strip().lower()
    decomposed = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in decomposed if not unicodedata.combining(c))
    if s in _TRUTHY:
        return "sim"
    if s in _FALSY:
        return "nao"
    return s


def resolve_path(collected: dict[str, Any], path: str) -> Any:
    """Resolve ``a.b.c`` em dicionário aninhado.

    Devolve :data:`MISSING` quando qualquer segmento falta — o chamador trata
    como "esse branch ainda não é roteável", diferente de "está vazio".
    """
    if not path:
        return MISSING
    cur: Any = collected
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return MISSING
    return cur


def is_filled(value: Any) -> bool:
    """Um slot conta como preenchido? ``None`` e string vazia não contam."""
    if value is MISSING or value is None:
        return False
    return not (isinstance(value, str) and not value.strip())
