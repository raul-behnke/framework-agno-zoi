"""O contrato com o Agno que a arquitetura assume.

Estes testes não testam código nosso — testam as premissas sobre o framework.
Se uma versão nova do Agno mudar qualquer uma delas, o pipeline inteiro para
de funcionar de um jeito difícil de diagnosticar. Melhor falhar aqui.

A premissa central, descoberta por experimento (Agno 3.0):

    Um step-função que declara ``run_context`` na assinatura recebe o
    ``session_state`` **vivo**. Mutação in-place propaga para os steps
    seguintes e é persistida no ``db`` por ``session_id``.

Não é o que a documentação mostra (ela usa ``step_input.session_state``, do
Agno 2). O Agno injeta kwargs por inspeção de assinatura: sem o parâmetro
``run_context``, a função simplesmente não recebe o estado.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from agno.db.sqlite import SqliteDb
from agno.workflow import Router, Step, StepInput, StepOutput, Workflow


def test_primitivas_do_workflow_existem() -> None:
    import agno.workflow as w

    for nome in (
        "Workflow",
        "Step",
        "Steps",
        "Loop",
        "Router",
        "Parallel",
        "Condition",
        "StepInput",
        "StepOutput",
    ):
        assert hasattr(w, nome), f"agno.workflow perdeu {nome}"


def test_step_aceita_workflow_aninhado() -> None:
    """``call_subroutine`` do routine YAML vira Workflow dentro de um Step."""
    import inspect

    assert "workflow" in inspect.signature(Step.__init__).parameters


def test_parametros_de_agent_usados_pelo_pipeline() -> None:
    import inspect

    from agno.agent import Agent

    sig = inspect.signature(Agent.__init__).parameters
    for p in (
        "output_schema",  # extrator e freetalk devolvem estrutura
        "post_hooks",  # guardas de saída (anti-invenção, frases proibidas)
        "instructions",  # persona
        "dependencies",  # contexto de grounding
        "db",  # sessão
        "add_history_to_context",
        "num_history_runs",
        "retries",
    ):
        assert p in sig, f"Agent perdeu o parâmetro {p}"


def test_modelo_litellm_disponivel() -> None:
    """O routing.yaml é escrito na semântica do LiteLLM."""
    from agno.models.litellm import LiteLLM

    assert LiteLLM is not None


# --------------------------------------------------------------------------
# O mecanismo de estado — a premissa que sustenta o executor determinístico
# --------------------------------------------------------------------------


@pytest.fixture
def wf_sonda(tmp_path: Path):
    """Workflow mínimo: um step que escreve, um Router que lê, um que confere."""
    visto: dict[str, object] = {}

    def escreve(step_input: StepInput, run_context) -> StepOutput:
        st = run_context.session_state
        visto["leu_no_inicio"] = {k: st.get(k) for k in ("contador", "cursor")}
        st["contador"] = st.get("contador", 0) + 1
        st["cursor"] = "n_dois"
        return StepOutput(content="escreveu")

    def confere(step_input: StepInput, run_context) -> StepOutput:
        st = run_context.session_state
        visto["viu_a_escrita"] = {k: st.get(k) for k in ("contador", "cursor")}
        return StepOutput(content="conferiu")

    def roteia(step_input: StepInput, run_context):
        visto["router_leu_cursor"] = run_context.session_state.get("cursor")
        return [Step(name="n_dois", executor=confere)]

    wf = Workflow(
        name="sonda",
        db=SqliteDb(db_file=str(tmp_path / "sonda.db")),
        session_state={"contador": 0, "cursor": "n_um"},
        steps=[
            Step(name="n_um", executor=escreve),
            Router(
                name="advance", selector=roteia, choices=[Step(name="n_dois", executor=confere)]
            ),
        ],
    )
    return wf, visto


def test_step_funcao_recebe_o_session_state_vivo(wf_sonda) -> None:
    wf, visto = wf_sonda
    wf.run(input="oi", session_id="s1")
    assert visto["leu_no_inicio"] == {"contador": 0, "cursor": "n_um"}, (
        "o step não recebeu o session_state inicial declarado no Workflow"
    )


def test_escrita_de_um_step_e_vista_pelos_seguintes(wf_sonda) -> None:
    """Sem isso, `apply` não consegue entregar slots ao `advance`."""
    wf, visto = wf_sonda
    wf.run(input="oi", session_id="s1")
    assert visto["viu_a_escrita"] == {"contador": 1, "cursor": "n_dois"}


def test_router_le_o_cursor_escrito_no_mesmo_turno(wf_sonda) -> None:
    """É assim que o executor determinístico move o grafo."""
    wf, visto = wf_sonda
    wf.run(input="oi", session_id="s1")
    assert visto["router_leu_cursor"] == "n_dois"


def test_estado_persiste_entre_turnos_da_mesma_sessao(wf_sonda) -> None:
    """Substitui o checkpointer do LangGraph."""
    wf, _ = wf_sonda
    wf.run(input="turno 1", session_id="s1")
    wf.run(input="turno 2", session_id="s1")
    wf.run(input="turno 3", session_id="s1")
    assert wf.get_session_state("s1")["contador"] == 3


def test_sessoes_diferentes_ficam_isoladas(wf_sonda) -> None:
    """Um lead não enxerga o estado de outro. session_id = thread da conversa."""
    wf, _ = wf_sonda
    wf.run(input="lead A", session_id="s1")
    wf.run(input="lead A de novo", session_id="s1")
    wf.run(input="lead B", session_id="s2")
    assert wf.get_session_state("s1")["contador"] == 2
    assert wf.get_session_state("s2")["contador"] == 1


def test_funcao_sem_run_context_nao_recebe_estado(tmp_path: Path) -> None:
    """A injeção é por assinatura — documenta a pegadinha que custou horas.

    Um executor que esquece de declarar ``run_context`` roda normalmente e
    não vê estado nenhum. Falha silenciosa, não exceção.
    """
    visto: dict[str, object] = {}

    def sem_contexto(step_input: StepInput) -> StepOutput:
        visto["workflow_session_tem_state"] = hasattr(step_input.workflow_session, "session_state")
        return StepOutput(content="ok")

    wf = Workflow(
        name="sem_ctx",
        db=SqliteDb(db_file=str(tmp_path / "x.db")),
        session_state={"a": 1},
        steps=[Step(name="n_um", executor=sem_contexto)],
    )
    wf.run(input="oi", session_id="s1")
    assert visto["workflow_session_tem_state"] is False
