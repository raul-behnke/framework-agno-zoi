"""O worker que acorda conversas paradas.

Uma conversa estacionada não ocupa processo: é uma linha no registro de
esperas. Este worker varre as vencidas, corrige o estado da sessão e reinvoca
o Workflow — que a partir daí roda um turno normal, com a mensagem do lead
vazia porque não foi ele quem provocou.

**Por que funciona no Agno sem nada especial.** O estado da conversa vive no
``session_state``, persistido por ``session_id``. Um processo externo abre o
mesmo banco, chama ``update_session_state`` e roda o Workflow. É a mesma
primitiva do turno normal — não há API de retomada, nem checkpoint a
reidratar. Era esta a pergunta que decidia se o modelo sobrevive fora do
LangGraph.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from zoi_agno.wait.repo import EsperaPendente, RepoDeEsperas

if TYPE_CHECKING:  # import só para tipagem — evita o ciclo
    # pipeline → wait.resolver → wait/__init__ → worker → builder → pipeline
    from zoi_agno.builder import WorkflowRuntime
    from zoi_agno.pipeline import Turno

logger = logging.getLogger(__name__)


@dataclass
class Retomada:
    """O que aconteceu ao acordar uma conversa."""

    session_id: str
    node_id: str
    causa: str
    turno: Turno | None = None
    erro: str = ""

    @property
    def ok(self) -> bool:
        return not self.erro


class WaitWorker:
    """Acorda conversas por prazo vencido ou por sinal externo.

    Recebe os runtimes por tenant já montados — construir um por retomada
    recarregaria routine, persona e catálogo a cada linha da fila.
    """

    def __init__(self, repo: RepoDeEsperas, runtimes: dict[str, WorkflowRuntime]) -> None:
        self.repo = repo
        self.runtimes = runtimes

    async def tick(self, agora: datetime | None = None, limite: int = 50) -> list[Retomada]:
        """Uma varredura: acorda tudo que venceu."""
        vencidas = self.repo.vencidas(agora, limite)
        return [await self.retomar(e, causa="timeout") for e in vencidas]

    async def sinalizar(self, tenant_id: str, topico: str, payload: Any = None) -> list[Retomada]:
        """Um evento externo chegou. Acorda quem esperava por este tópico."""
        esperas = self.repo.por_topico(tenant_id, topico)
        return [await self.retomar(e, causa="signal", payload=payload) for e in esperas]

    async def retomar(self, espera: EsperaPendente, *, causa: str, payload: Any = None) -> Retomada:
        """Acorda uma conversa: corrige o estado e roda um turno.

        A espera é concluída ANTES de rodar o turno. Se o turno falhar, a
        conversa fica parada em vez de ser reacordada em laço a cada varredura
        — retomada em loop é pior que retomada perdida, porque o lead recebe a
        mesma mensagem várias vezes.
        """
        r = Retomada(session_id=espera.session_id, node_id=espera.retomar_em, causa=causa)
        rt = self.runtimes.get(espera.tenant_id)
        if rt is None:
            r.erro = f"sem runtime para o tenant {espera.tenant_id!r}"
            logger.error("wait.sem_runtime tenant=%s", espera.tenant_id)
            self.repo.concluir(espera.id)
            return r

        self.repo.concluir(espera.id)
        try:
            rt.workflow.update_session_state(
                {
                    "current_node": espera.retomar_em,
                    "turns_in_node": 0,
                    "_waiting": False,
                    "_acordou_por": causa,
                    "_sinal_externo": payload,
                },
                session_id=espera.session_id,
            )
            # Mensagem vazia: não foi o lead que provocou este turno.
            r.turno = await rt.turno(espera.session_id, "")
        except Exception as exc:  # noqa: BLE001 — uma retomada ruim não para a fila
            r.erro = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "wait.retomada_falhou session=%s node=%s err=%r",
                espera.session_id,
                espera.retomar_em,
                exc,
            )
        else:
            logger.info(
                "wait.retomado session=%s node=%s causa=%s",
                espera.session_id,
                espera.retomar_em,
                causa,
            )
        return r


def agora_utc() -> datetime:
    return datetime.now(UTC)
