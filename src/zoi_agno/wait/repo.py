"""Registro de esperas pendentes.

Uma conversa parada num nó ``wait`` não ocupa processo nenhum: ela vira uma
linha aqui, e um worker externo a acorda quando o prazo vence ou o sinal
chega.

Tabela própria, e não uma coluna no estado da sessão, por um motivo prático:
para acordar é preciso **procurar** — "quais conversas venceram?" — e varrer
o estado de todas as sessões não escala.

O contrato é o mínimo que o worker precisa. Trocar SQLite por Postgres é
implementar a mesma interface; nada acima daqui sabe qual é.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class EsperaPendente:
    """Uma conversa estacionada, esperando prazo ou sinal."""

    id: int
    tenant_id: str
    session_id: str
    node_id: str
    """Onde a conversa parou — o nó ``wait``."""

    retomar_em: str
    """Destino da retomada por prazo."""

    vence_em: datetime | None
    """Quando o prazo estoura. ``None`` = espera só por sinal."""

    topico: str | None = None
    """Tópico do sinal que acorda esta espera, quando houver."""


class RepoDeEsperas(Protocol):
    """O que o worker precisa. Implementável sobre qualquer banco."""

    def estacionar(
        self,
        *,
        tenant_id: str,
        session_id: str,
        node_id: str,
        retomar_em: str,
        vence_em: datetime | None,
        topico: str | None = None,
    ) -> int: ...

    def vencidas(self, agora: datetime | None = None, limite: int = 50) -> list[EsperaPendente]: ...

    def por_topico(self, tenant_id: str, topico: str) -> list[EsperaPendente]: ...

    def concluir(self, espera_id: int) -> None: ...

    def pendentes_da_sessao(self, session_id: str) -> list[EsperaPendente]: ...


_DDL = """
CREATE TABLE IF NOT EXISTS esperas (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id   TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    node_id     TEXT NOT NULL,
    retomar_em  TEXT NOT NULL,
    vence_em    TEXT,
    topico      TEXT,
    criada_em   TEXT NOT NULL,
    concluida   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_esperas_vence ON esperas (concluida, vence_em);
CREATE INDEX IF NOT EXISTS ix_esperas_topico ON esperas (concluida, tenant_id, topico);
"""


class RepoSQLite:
    """Implementação em SQLite. Suficiente para desenvolvimento e fixture."""

    def __init__(self, db_file: str | Path) -> None:
        self.db_file = str(db_file)
        with self._conn() as c:
            c.executescript(_DDL)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_file)
        c.row_factory = sqlite3.Row
        return c

    def estacionar(
        self,
        *,
        tenant_id: str,
        session_id: str,
        node_id: str,
        retomar_em: str,
        vence_em: datetime | None,
        topico: str | None = None,
    ) -> int:
        """Registra a espera. Uma sessão só pode ter uma espera aberta.

        Estacionar de novo fecha a anterior: se a conversa andou e parou noutro
        ponto, a espera velha acordaria num nó que não é mais o atual.
        """
        with self._conn() as c:
            c.execute(
                "UPDATE esperas SET concluida = 1 WHERE session_id = ? AND concluida = 0",
                (session_id,),
            )
            cur = c.execute(
                "INSERT INTO esperas "
                "(tenant_id, session_id, node_id, retomar_em, vence_em, topico, criada_em) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    tenant_id,
                    session_id,
                    node_id,
                    retomar_em,
                    vence_em.isoformat() if vence_em else None,
                    topico,
                    datetime.now(UTC).isoformat(),
                ),
            )
            return int(cur.lastrowid or 0)

    def vencidas(self, agora: datetime | None = None, limite: int = 50) -> list[EsperaPendente]:
        """Esperas cujo prazo já passou."""
        marca = (agora or datetime.now(UTC)).isoformat()
        with self._conn() as c:
            linhas = c.execute(
                "SELECT * FROM esperas WHERE concluida = 0 AND vence_em IS NOT NULL "
                "AND vence_em <= ? ORDER BY vence_em LIMIT ?",
                (marca, limite),
            ).fetchall()
        return [_de_linha(x) for x in linhas]

    def por_topico(self, tenant_id: str, topico: str) -> list[EsperaPendente]:
        with self._conn() as c:
            linhas = c.execute(
                "SELECT * FROM esperas WHERE concluida = 0 AND tenant_id = ? AND topico = ?",
                (tenant_id, topico),
            ).fetchall()
        return [_de_linha(x) for x in linhas]

    def concluir(self, espera_id: int) -> None:
        with self._conn() as c:
            c.execute("UPDATE esperas SET concluida = 1 WHERE id = ?", (espera_id,))

    def pendentes_da_sessao(self, session_id: str) -> list[EsperaPendente]:
        with self._conn() as c:
            linhas = c.execute(
                "SELECT * FROM esperas WHERE concluida = 0 AND session_id = ?", (session_id,)
            ).fetchall()
        return [_de_linha(x) for x in linhas]


def _de_linha(x: sqlite3.Row) -> EsperaPendente:
    return EsperaPendente(
        id=int(x["id"]),
        tenant_id=str(x["tenant_id"]),
        session_id=str(x["session_id"]),
        node_id=str(x["node_id"]),
        retomar_em=str(x["retomar_em"]),
        vence_em=datetime.fromisoformat(x["vence_em"]) if x["vence_em"] else None,
        topico=x["topico"],
    )
