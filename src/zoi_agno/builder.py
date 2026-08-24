"""``Tenant`` → ``agno.Workflow``.

Aqui a decisão central do projeto vira código: os estágios do turno são
**Steps do Workflow**, e o estado da conversa é o ``session_state`` que o Agno
persiste no ``db`` por ``session_id``. Não há checkpointer próprio, não há
LangGraph.

    Step("ingress")   função — abre o turno, monta o prompt do extrator
    Step("extract")   Agent  🤖 output_schema=CommandGenOutput
    Step("processar") função — fiscaliza, aplica, avança, executa tools
    Step("compose")   Agent  🤖 persona + grounding
    Step("finalizar") função — guardas e fechamento

**Por que cinco steps e não um por nó do roteiro.** O grafo da conversa é
interpretado em runtime pelo executor; a topologia do Workflow é a do *turno*,
que é sempre a mesma. Um Step por nó exigiria recompilar o Workflow a cada
routine publicada e ainda assim não expressaria o roteamento — que depende de
estado, não de posição. Um ``Router`` aqui seria decoração: ele escolheria
sempre o mesmo caminho.

**Por que os steps-função declaram ``run_context``.** É como o Agno injeta o
``session_state`` vivo. Sem esse parâmetro a função roda e não vê estado
nenhum, em silêncio — ver ``tests/test_agno_contract.py``.
"""

from __future__ import annotations

import logging
from typing import Any

from agno.workflow import Step, StepInput, StepOutput, Workflow

from zoi_agno.pipeline import Pipeline, Turno
from zoi_agno.state import new_session_state
from zoi_agno.tenants import Tenant

logger = logging.getLogger(__name__)

# Chaves que o pipeline usa para passar dados de um step ao seguinte dentro do
# mesmo turno. Vivem no session_state porque é o único canal que o Agno
# garante entre steps; são limpas no fim do turno.
_MEIO = "_meio_do_turno"
_TURNO = "_turno_resultado"


def build_workflow(tenant: Tenant, *, db: Any, pipeline: Pipeline | None = None) -> Workflow:
    """Monta o Workflow de um tenant.

    ``pipeline`` pode ser injetado nos testes para trocar os cérebros por
    dublês sem tocar na topologia.
    """
    p = pipeline or Pipeline(tenant, db=db)

    async def ingress(step_input: StepInput, run_context) -> StepOutput:
        estado = run_context.session_state
        _garantir_estado(estado, tenant)
        user_msg = str(step_input.input or "")
        prompt = p.ingress(estado, user_msg)
        # O planner roda aqui, no mesmo lugar em que ``rodar_turno`` o chama.
        # Ligá-lo num caminho só faria os dois divergirem — foi o que
        # aconteceu na primeira versão deste arquivo.
        await p.planejar(estado, user_msg)
        return StepOutput(content=prompt)

    async def processar(step_input: StepInput, run_context) -> StepOutput:
        estado = run_context.session_state
        meio = await p.processar(estado, step_input.previous_step_content)
        # O resultado do executor não é serializável para o histórico do
        # Workflow; guardamos no estado e devolvemos só o prompt.
        estado[_MEIO] = meio
        return StepOutput(content=meio["prompt_redator"])

    async def finalizar(step_input: StepInput, run_context) -> StepOutput:
        estado = run_context.session_state
        meio = estado.pop(_MEIO, None)
        if meio is None:  # pragma: no cover — só se um step for pulado
            logger.error("builder.finalizar_sem_meio session=%s", estado.get("thread_id"))
            return StepOutput(content=str(step_input.previous_step_content or ""))
        turno = await p.finalizar(estado, str(step_input.previous_step_content or ""), meio)
        estado[_TURNO] = {
            "texto": turno.texto,
            "node_id": turno.node_id,
            "finished": turno.finished,
            "handoff": turno.handoff,
        }
        return StepOutput(content=turno.texto)

    return Workflow(
        name=tenant.routine.routine_name,
        db=db,
        session_state=new_session_state(
            thread_id="",
            tenant_id=tenant.tenant_id,
            contact_id="",
            start_node=tenant.start_node,
            routine_version=tenant.routine_version,
        ),
        steps=[
            Step(name="ingress", executor=ingress),
            Step(name="extract", agent=p.extrator),
            Step(name="processar", executor=processar),
            Step(name="compose", agent=p.redator),
            Step(name="finalizar", executor=finalizar),
        ],
    )


def _garantir_estado(estado: dict[str, Any], tenant: Tenant) -> None:
    """Completa o estado numa sessão nova.

    O ``session_state`` do Workflow é um molde compartilhado por todas as
    sessões; o Agno o copia ao criar cada uma. Aqui carimbamos o que só se
    sabe no primeiro turno daquela conversa.
    """
    if not estado.get("current_node"):
        estado.update(
            new_session_state(
                thread_id=estado.get("thread_id", ""),
                tenant_id=tenant.tenant_id,
                contact_id=estado.get("contact_id", ""),
                start_node=tenant.start_node,
                routine_version=tenant.routine_version,
            )
        )


class WorkflowRuntime:
    """Roda turnos através do Workflow do Agno, com estado persistido.

    É esta a fronteira que um canal usa: dá o ``session_id`` da conversa e a
    mensagem do lead, recebe o que responder. Quem carrega e grava o estado é
    o Agno.
    """

    def __init__(self, tenant: Tenant, *, db: Any, pipeline: Pipeline | None = None) -> None:
        self.tenant = tenant
        self.db = db
        self.workflow = build_workflow(tenant, db=db, pipeline=pipeline)

    async def turno(self, session_id: str, user_msg: str) -> Turno:
        """Um turno. O estado vem do ``db`` e volta para ele."""
        await self.workflow.arun(input=user_msg, session_id=session_id)
        estado = self.workflow.get_session_state(session_id)
        dados = estado.get(_TURNO) or {}
        return Turno(
            texto=str(dados.get("texto", "")),
            node_id=str(dados.get("node_id", "")),
            finished=bool(dados.get("finished")),
            handoff=bool(dados.get("handoff")),
        )

    def estado(self, session_id: str) -> dict[str, Any]:
        """O ``session_state`` persistido da conversa."""
        return self.workflow.get_session_state(session_id)
