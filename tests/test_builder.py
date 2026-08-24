"""O pipeline como Workflow do Agno — a decisão central do projeto.

Aqui se verifica que os estágios do turno são Steps de verdade e que o estado
da conversa vive no ``session_state``, persistido pelo Agno por ``session_id``.
Nenhum checkpointer próprio, nenhum LangGraph.

Os cérebros são dublês: o que está sob teste é a topologia e a costura do
estado, não a qualidade do texto.
"""

from __future__ import annotations

from typing import Any

import pytest
from agno.agent import Agent
from agno.db.sqlite import SqliteDb
from agno.models.litellm import LiteLLM
from agno.run.agent import RunOutput

from zoi_agno.builder import WorkflowRuntime, build_workflow
from zoi_agno.contracts import CommandGenOutput
from zoi_agno.tenants import load_tenant

from .conftest import FIXTURES_TENANTS, pipeline_dublado


class AgenteFalso(Agent):
    """Dublê de cérebro: um ``Agent`` de verdade que não chama modelo.

    Herda de ``Agent`` em vez de imitar sua interface porque o ``Step`` lê
    vários atributos do executor (``store_media``, ``scrub_run_output_for_storage``,
    ...). Um objeto improvisado falha nesses acessos, o Step trata como erro,
    repete o step, e o sintoma chega como conteúdo vazio — não como exceção.
    """

    def __init__(self, respostas: list[Any], nome: str = "falso") -> None:
        super().__init__(name=nome, model=LiteLLM(id="gpt-4o-mini"))
        self._respostas = list(respostas)
        self.chamadas: list[str] = []

    def _proxima(self, entrada: str) -> RunOutput:
        self.chamadas.append(entrada)
        return RunOutput(content=self._respostas.pop(0) if self._respostas else None)

    async def arun(self, input: str = "", **_: Any) -> RunOutput:  # type: ignore[override]
        return self._proxima(input)

    def run(self, input: str = "", **_: Any) -> RunOutput:  # type: ignore[override]
        return self._proxima(input)


def _lote(*comandos: dict[str, Any]) -> CommandGenOutput:
    return CommandGenOutput.model_validate({"commands": list(comandos)})


def _slot(nome: str, valor: Any) -> dict[str, Any]:
    return {"kind": "set_slot", "payload": {"slot": nome, "value": valor}}


@pytest.fixture
def runtime(tmp_path):
    t = load_tenant("t_demo", base_dir=FIXTURES_TENANTS)
    p = pipeline_dublado(t)
    p.extrator = AgenteFalso(
        [
            _lote(_slot("nome", "Ana"), _slot("servico", "corte")),
            _lote(
                _slot("horario", "terça às 11:00"),
                {"kind": "signal", "payload": {"name": "escolheu", "value": True}},
            ),
        ],
        nome="extrator",
    )
    p.redator = AgenteFalso(["Combinado!", "Fechado!"], nome="redator")
    db = SqliteDb(db_file=str(tmp_path / "wf.db"))
    return WorkflowRuntime(t, db=db, pipeline=p), p


# --------------------------------------------------------------------------
# topologia
# --------------------------------------------------------------------------


def test_o_workflow_tem_os_cinco_estagios(tmp_path) -> None:
    t = load_tenant("t_demo", base_dir=FIXTURES_TENANTS)
    wf = build_workflow(
        t, db=SqliteDb(db_file=str(tmp_path / "x.db")), pipeline=pipeline_dublado(t)
    )
    assert [s.name for s in wf.steps] == [
        "ingress",
        "extract",
        "processar",
        "compose",
        "finalizar",
    ]


def test_llm_so_nos_steps_de_agente(tmp_path) -> None:
    """A fronteira: step-função nunca chama modelo; step-agente sempre chama."""
    t = load_tenant("t_demo", base_dir=FIXTURES_TENANTS)
    wf = build_workflow(
        t, db=SqliteDb(db_file=str(tmp_path / "x.db")), pipeline=pipeline_dublado(t)
    )
    por_nome = {s.name: s for s in wf.steps}
    for nome in ("ingress", "processar", "finalizar"):
        assert por_nome[nome].active_executor is not None
        assert por_nome[nome]._executor_type == "function"
    for nome in ("extract", "compose"):
        assert por_nome[nome]._executor_type == "agent"


def test_o_workflow_leva_o_nome_da_routine(tmp_path) -> None:
    t = load_tenant("t_demo", base_dir=FIXTURES_TENANTS)
    wf = build_workflow(
        t, db=SqliteDb(db_file=str(tmp_path / "x.db")), pipeline=pipeline_dublado(t)
    )
    assert wf.name == "demo_barbearia"


# --------------------------------------------------------------------------
# estado
# --------------------------------------------------------------------------


async def test_um_turno_atravessa_os_cinco_steps(runtime) -> None:
    rt, p = runtime
    r = await rt.turno("s1", "sou a Ana, quero corte")
    assert r.texto == "Combinado!"
    assert len(p.extrator.chamadas) == 1
    assert len(p.redator.chamadas) == 1


async def test_o_estado_persiste_entre_turnos(runtime) -> None:
    """Substitui o checkpointer: quem guarda é o db do Agno."""
    rt, _ = runtime
    await rt.turno("s1", "sou a Ana, quero corte")
    st = rt.estado("s1")
    assert st["collected"]["nome"] == "Ana"
    assert st["_turn_counter"] == 1

    await rt.turno("s1", "terça às 11")
    st = rt.estado("s1")
    assert st["_turn_counter"] == 2
    assert st["collected"]["horario"] == "terça às 11:00"


async def test_a_conversa_chega_ao_fim_pelo_workflow(runtime) -> None:
    rt, _ = runtime
    await rt.turno("s1", "sou a Ana, quero corte")
    r = await rt.turno("s1", "terça às 11")
    assert r.finished is True
    assert r.node_id == "e_agendado"


async def test_sessoes_diferentes_nao_se_misturam(tmp_path) -> None:
    """``session_id`` é a thread da conversa: um lead não vê o estado de outro."""
    t = load_tenant("t_demo", base_dir=FIXTURES_TENANTS)
    p = pipeline_dublado(t)
    p.extrator = AgenteFalso([_lote(_slot("nome", "Ana")), _lote(_slot("nome", "Bruno"))])
    p.redator = AgenteFalso(["oi", "oi"])
    rt = WorkflowRuntime(t, db=SqliteDb(db_file=str(tmp_path / "wf.db")), pipeline=p)

    await rt.turno("lead-a", "sou a Ana")
    await rt.turno("lead-b", "sou o Bruno")

    assert rt.estado("lead-a")["collected"]["nome"] == "Ana"
    assert rt.estado("lead-b")["collected"]["nome"] == "Bruno"
    assert rt.estado("lead-a")["_turn_counter"] == 1


async def test_a_fiscalizacao_roda_dentro_do_workflow(tmp_path) -> None:
    """Os Steps não são um caminho paralelo que escapa das rules."""
    t = load_tenant("t_demo", base_dir=FIXTURES_TENANTS)
    p = pipeline_dublado(t)
    p.extrator = AgenteFalso([_lote(_slot("cpf_do_avo", "123"))])
    p.redator = AgenteFalso(["ok"])
    rt = WorkflowRuntime(t, db=SqliteDb(db_file=str(tmp_path / "wf.db")), pipeline=p)

    await rt.turno("s1", "meu avô tem cpf")

    st = rt.estado("s1")
    assert "cpf_do_avo" not in st["collected"]


async def test_a_chave_de_transporte_nao_vaza_para_o_estado(runtime) -> None:
    """O dado que atravessa os steps é limpo no fim do turno."""
    rt, _ = runtime
    await rt.turno("s1", "sou a Ana, quero corte")
    st = rt.estado("s1")
    assert "_meio_do_turno" not in st


async def test_o_workflow_e_o_rodar_turno_chamam_os_mesmos_cerebros(tmp_path) -> None:
    """Os dois caminhos não podem divergir.

    Regressão real: o planner foi ligado em ``rodar_turno`` e esquecido nos
    steps do Workflow. Nada quebrou — o plano simplesmente nunca era gerado no
    caminho que a produção usa, e o sintoma era um campo vazio no estado.
    """
    t = load_tenant("t_demo", base_dir=FIXTURES_TENANTS)

    class PlanejadorContador(AgenteFalso):
        chamado = 0

        async def arun(self, input: str = "", **_: Any) -> RunOutput:
            type(self).chamado += 1
            return RunOutput(content=None)

    def monta():
        p = pipeline_dublado(
            t,
            extrator=AgenteFalso([_lote(_slot("nome", "Ana"))]),
            redator=AgenteFalso(["ok"]),
        )
        p.planejador = PlanejadorContador([], "planner")
        return p

    # caminho A: rodar_turno
    PlanejadorContador.chamado = 0
    pa = monta()
    from zoi_agno.state import new_session_state

    st = new_session_state(
        thread_id="a", tenant_id="t_demo", contact_id="1", start_node=t.start_node
    )
    await pa.rodar_turno(st, "sou a Ana")
    via_classe = PlanejadorContador.chamado

    # caminho B: Workflow
    PlanejadorContador.chamado = 0
    rt = WorkflowRuntime(t, db=SqliteDb(db_file=str(tmp_path / "wf.db")), pipeline=monta())
    await rt.turno("b", "sou a Ana")
    via_workflow = PlanejadorContador.chamado

    assert via_classe == via_workflow == 1, (
        f"planner chamado {via_classe}x pela classe e {via_workflow}x pelo Workflow"
    )
