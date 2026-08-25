"""Esperas duráveis — o critério de desistência do projeto.

No grill, o critério foi: *"o Workflow do Agno não dá conta de `wait` sem
gambiarra"*. Este arquivo responde. A conversa estaciona, some do processo, e
um worker externo — outro objeto, sobre o mesmo banco — a acorda e ela
continua de onde parou.

Nenhuma API de retomada, nenhum checkpoint a reidratar: é o mesmo
``session_state`` do turno normal.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from agno.db.sqlite import SqliteDb
from zoi_routine import parse_routine

from zoi_agno.builder import WorkflowRuntime
from zoi_agno.executor import advance
from zoi_agno.executor.advance import WaitSemRepo
from zoi_agno.state import new_session_state
from zoi_agno.tenants import Tenant
from zoi_agno.wait import DuracaoInvalida, RepoSQLite, WaitWorker, duracao, resolver

from .conftest import pipeline_dublado
from .test_builder import AgenteFalso, _lote

# Um follow-up: o lead não pode falar agora, o agente promete voltar.
ROTEIRO = """routine_name: t_espera
version: 1
slots:
  nome: { type: string }
main:
  start: w_followup
  nodes:
    w_followup:
      type: wait
      name: "Aguardando o lead"
      mode: user
      timeout: PT24H
      on_timeout: c_retomada
      next: c_retomada
    c_retomada:
      type: collect_group
      group_name: retomada
      exit_policy: all
      max_turns: 3
      fields:
        - { name: nome, required: true, question: "voltando: como é seu nome?" }
      next: e_fim
    e_fim:
      type: end
      role: success
      farewell: "Combinado!"
"""


def _tenant(tmp_path) -> Tenant:
    return Tenant(tenant_id="t_espera", routine=parse_routine(ROTEIRO), dir=tmp_path)


# --------------------------------------------------------------------------
# duração
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("iso", "esperado"),
    [
        ("PT24H", timedelta(hours=24)),
        ("PT30M", timedelta(minutes=30)),
        ("P3D", timedelta(days=3)),
        ("P1DT2H30M", timedelta(days=1, hours=2, minutes=30)),
    ],
)
def test_duracao_iso(iso: str, esperado: timedelta) -> None:
    assert duracao(iso) == esperado


@pytest.mark.parametrize("ruim", ["24h", "PT", "P", "", "amanhã"])
def test_duracao_invalida_falha_alto(ruim: str) -> None:
    """Prazo ilegível é erro de autoria — melhor não subir que acordar errado."""
    with pytest.raises(DuracaoInvalida):
        duracao(ruim)


# --------------------------------------------------------------------------
# resolução do nó
# --------------------------------------------------------------------------


def _no_wait(corpo: str):
    """Monta um routine mínimo com um único nó ``wait`` e devolve o nó."""
    yaml = (
        "routine_name: t_resolver\nversion: 1\nslots:\n  campo: { type: string }\n"
        "main:\n  start: w_um\n  nodes:\n"
        f"    w_um:\n      type: wait\n{corpo}"
        '    e_fim:\n      type: end\n      role: success\n      farewell: "fim"\n'
    )
    return parse_routine(yaml).main.nodes["w_um"]


def test_modo_user_vence_pelo_prazo() -> None:
    agora = datetime(2026, 9, 1, tzinfo=UTC)
    node = _no_wait(
        "      mode: user\n      timeout: PT24H\n      on_timeout: e_fim\n      next: e_fim\n"
    )
    e = resolver(node, "w_um", {}, agora)
    assert e.vence_em == agora + timedelta(hours=24)
    assert e.retomar_em == "e_fim"
    assert e.topico is None


def test_modo_signal_espera_sem_prazo() -> None:
    node = _no_wait(
        "      mode: signal\n      signal_topic: pagamento_ok\n"
        "      on_signal: e_fim\n      next: e_fim\n"
    )
    e = resolver(node, "w_um", {})
    assert e.vence_em is None, "espera por sinal não expira sozinha"
    assert e.topico == "pagamento_ok"


def test_boundary_de_timer_encurta_o_prazo() -> None:
    """O boundary existe justamente para cortar antes do prazo do nó."""
    agora = datetime(2026, 9, 1, tzinfo=UTC)
    node = _no_wait(
        "      mode: user\n      timeout: PT24H\n      on_timeout: e_fim\n"
        "      boundaries:\n        - { kind: timer, duration: PT2H, target: e_fim }\n"
        "      next: e_fim\n"
    )
    e = resolver(node, "w_um", {}, agora)
    assert e.vence_em == agora + timedelta(hours=2)


# --------------------------------------------------------------------------
# o executor estaciona
# --------------------------------------------------------------------------


def test_o_executor_estaciona_sem_mover_o_cursor() -> None:
    routine = parse_routine(ROTEIRO)
    st = new_session_state(thread_id="x", tenant_id="t", contact_id="1", start_node="w_followup")
    r = advance(routine, st)
    assert r.waiting is not None
    assert st["_waiting"] is True
    assert st["current_node"] == "w_followup"
    assert r.finished is False


async def test_roteiro_com_wait_sem_repo_falha_alto(tmp_path) -> None:
    """Melhor o tenant não subir que o lead nunca receber o follow-up."""
    p = pipeline_dublado(
        _tenant(tmp_path),
        extrator=AgenteFalso([_lote()]),
        redator=AgenteFalso(["ok"]),
    )
    st = new_session_state(
        thread_id="s1", tenant_id="t_espera", contact_id="1", start_node="w_followup"
    )
    with pytest.raises(WaitSemRepo):
        await p.rodar_turno(st, "oi")


# --------------------------------------------------------------------------
# registro
# --------------------------------------------------------------------------


def test_estacionar_e_encontrar_por_vencimento(tmp_path) -> None:
    repo = RepoSQLite(tmp_path / "w.db")
    agora = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    repo.estacionar(
        tenant_id="t",
        session_id="s1",
        node_id="w",
        retomar_em="n",
        vence_em=agora + timedelta(hours=1),
    )
    assert repo.vencidas(agora) == []
    assert len(repo.vencidas(agora + timedelta(hours=2))) == 1


def test_estacionar_de_novo_fecha_a_espera_anterior(tmp_path) -> None:
    """A conversa andou e parou noutro ponto: a espera velha acordaria errado."""
    repo = RepoSQLite(tmp_path / "w.db")
    agora = datetime(2026, 9, 1, tzinfo=UTC)
    repo.estacionar(tenant_id="t", session_id="s1", node_id="w1", retomar_em="a", vence_em=agora)
    repo.estacionar(tenant_id="t", session_id="s1", node_id="w2", retomar_em="b", vence_em=agora)
    pendentes = repo.pendentes_da_sessao("s1")
    assert len(pendentes) == 1
    assert pendentes[0].node_id == "w2"


def test_espera_por_topico(tmp_path) -> None:
    repo = RepoSQLite(tmp_path / "w.db")
    repo.estacionar(
        tenant_id="t",
        session_id="s1",
        node_id="w",
        retomar_em="n",
        vence_em=None,
        topico="pagamento_ok",
    )
    assert len(repo.por_topico("t", "pagamento_ok")) == 1
    assert repo.por_topico("t", "outro") == []
    assert repo.vencidas() == [], "espera por sinal não aparece entre as vencidas"


# --------------------------------------------------------------------------
# a prova: estaciona, some, e o worker acorda
# --------------------------------------------------------------------------


@pytest.fixture
def cenario(tmp_path):
    t = _tenant(tmp_path)
    repo = RepoSQLite(tmp_path / "esperas.db")
    p = pipeline_dublado(
        t,
        extrator=AgenteFalso([_lote(), _lote(), _lote()]),
        redator=AgenteFalso(["ok", "voltei!", "e aí?"]),
    )
    p.repo_de_esperas = repo
    rt = WorkflowRuntime(t, db=SqliteDb(db_file=str(tmp_path / "wf.db")), pipeline=p)
    return t, repo, rt


async def test_a_conversa_estaciona_e_vira_uma_linha(cenario) -> None:
    _t, repo, rt = cenario
    await rt.turno("s1", "oi")
    pendentes = repo.pendentes_da_sessao("s1")
    assert len(pendentes) == 1
    assert pendentes[0].retomar_em == "c_retomada"
    assert rt.estado("s1")["_waiting"] is True


async def test_o_worker_acorda_a_conversa_no_prazo(cenario) -> None:
    """A prova do critério de desistência.

    Nada roda entre o estacionamento e a retomada: a conversa existe só como
    estado no banco. O worker a traz de volta com a mesma primitiva do turno
    normal.
    """
    t, repo, rt = cenario
    await rt.turno("s1", "oi, não posso falar agora")
    assert rt.estado("s1")["_waiting"] is True

    worker = WaitWorker(repo, {t.tenant_id: rt})
    # Nada vence agora.
    assert await worker.tick(datetime.now(UTC)) == []

    # Amanhã, vence.
    retomadas = await worker.tick(datetime.now(UTC) + timedelta(hours=25))
    assert len(retomadas) == 1
    r = retomadas[0]
    assert r.ok, r.erro
    assert r.causa == "timeout"

    estado = rt.estado("s1")
    assert estado["_waiting"] is False
    assert estado["_acordou_por"] == "timeout"
    assert estado["current_node"] == "c_retomada", "acordou no destino declarado"
    assert repo.pendentes_da_sessao("s1") == [], "a espera foi concluída"


async def test_a_espera_e_concluida_antes_do_turno(cenario) -> None:
    """Retomada em loop é pior que retomada perdida.

    Se o turno falha e a espera continua aberta, toda varredura reacorda a
    conversa e o lead recebe a mesma mensagem várias vezes.
    """
    t, repo, rt = cenario
    await rt.turno("s1", "oi")

    async def turno_que_quebra(*_a, **_k):
        raise RuntimeError("provedor fora do ar")

    rt.turno = turno_que_quebra  # type: ignore[method-assign]
    worker = WaitWorker(repo, {t.tenant_id: rt})
    retomadas = await worker.tick(datetime.now(UTC) + timedelta(hours=25))

    assert not retomadas[0].ok
    assert repo.pendentes_da_sessao("s1") == [], "não pode reacordar em laço"


async def test_sinal_externo_acorda_quem_esperava_o_topico(tmp_path) -> None:
    repo = RepoSQLite(tmp_path / "e.db")
    t = _tenant(tmp_path)
    p = pipeline_dublado(
        t, extrator=AgenteFalso([_lote(), _lote()]), redator=AgenteFalso(["ok", "voltei"])
    )
    p.repo_de_esperas = repo
    rt = WorkflowRuntime(t, db=SqliteDb(db_file=str(tmp_path / "wf.db")), pipeline=p)

    # A sessão precisa existir: o worker corrige um estado, não o cria.
    await rt.turno("s9", "oi")
    repo.estacionar(
        tenant_id="t_espera",
        session_id="s9",
        node_id="w_followup",
        retomar_em="c_retomada",
        vence_em=None,
        topico="pagamento_ok",
    )

    retomadas = await WaitWorker(repo, {"t_espera": rt}).sinalizar(
        "t_espera", "pagamento_ok", payload={"valor": 100}
    )

    assert len(retomadas) == 1 and retomadas[0].ok
    estado = rt.estado("s9")
    assert estado["_acordou_por"] == "signal"
    assert estado["_sinal_externo"] == {"valor": 100}


async def test_retomada_sem_runtime_do_tenant_nao_derruba_a_fila(tmp_path) -> None:
    repo = RepoSQLite(tmp_path / "e.db")
    repo.estacionar(
        tenant_id="t_fantasma",
        session_id="s1",
        node_id="w",
        retomar_em="n",
        vence_em=datetime(2020, 1, 1, tzinfo=UTC),
    )
    retomadas = await WaitWorker(repo, {}).tick()
    assert not retomadas[0].ok
    assert "sem runtime" in retomadas[0].erro
    assert repo.pendentes_da_sessao("s1") == []
