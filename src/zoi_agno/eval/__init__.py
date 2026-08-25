"""Avaliação — o instrumento que diz se o runtime se comporta.

Os goldens são listas de falas do lead, no mesmo formato do v4, para que o
mesmo dado possa rodar nos dois runtimes e ser comparado.
"""

from __future__ import annotations

from zoi_agno.eval.goldens import (
    Relatorio,
    ResultadoGolden,
    Violacao,
    carregar_goldens,
    reproduzir,
    rodar_suite,
)

__all__ = [
    "Relatorio",
    "ResultadoGolden",
    "Violacao",
    "carregar_goldens",
    "reproduzir",
    "rodar_suite",
]
