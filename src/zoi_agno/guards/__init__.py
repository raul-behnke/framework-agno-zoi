"""Guardas de saída — o que o agente NÃO pode dizer.

Rodam depois da geração, sobre o texto pronto. São determinísticos: uma
verificação é mais barata e mais confiável que um pedido no prompt, e o
histórico do v4 mostra que pedir "não invente" não segura modelo nenhum.
"""

from __future__ import annotations

from zoi_agno.guards.fabrication import Violacao, checar_grounding, extrair_codigos
from zoi_agno.guards.phrases import checar_frases_proibidas
from zoi_agno.guards.signals import limpar_nomes_de_sinal

__all__ = [
    "Violacao",
    "checar_frases_proibidas",
    "checar_grounding",
    "extrair_codigos",
    "limpar_nomes_de_sinal",
]
