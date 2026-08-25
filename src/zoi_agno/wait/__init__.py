"""Esperas duráveis — conversa parada não ocupa processo.

Um nó ``wait`` estaciona a conversa e registra uma linha; um worker externo a
acorda quando o prazo vence ou o sinal chega.
"""

from __future__ import annotations

from zoi_agno.wait.repo import EsperaPendente, RepoDeEsperas, RepoSQLite
from zoi_agno.wait.resolver import DuracaoInvalida, Espera, duracao, resolver
from zoi_agno.wait.worker import Retomada, WaitWorker

__all__ = [
    "DuracaoInvalida",
    "Espera",
    "EsperaPendente",
    "RepoDeEsperas",
    "RepoSQLite",
    "Retomada",
    "WaitWorker",
    "duracao",
    "resolver",
]
