"""Conversa com LLM de verdade — o teste caro.

Roda só com ``OPENAI_API_KEY`` no ambiente. Não afirma texto literal (o modelo
varia), e sim as propriedades que precisam valer sempre:

- o que o lead disse vira estado
- o que o agente oferece veio de um payload, não da imaginação
- o sinal do lead move o fluxo até o desfecho certo

É o menor teste que falharia se o modelo de composição parasse de funcionar
sobre o Agno.
"""

from __future__ import annotations

import os

import pytest

from zoi_agno.pipeline import Pipeline
from zoi_agno.state import new_session_state
from zoi_agno.tenants import load_tenant

from .conftest import FIXTURES_TENANTS

pytestmark = pytest.mark.skipif(
    not os.getenv("OPENAI_API_KEY"), reason="exige OPENAI_API_KEY (teste com custo real)"
)


@pytest.fixture
def pipe():
    t = load_tenant("t_demo", base_dir=FIXTURES_TENANTS)
    st = new_session_state(
        thread_id="live:1", tenant_id=t.tenant_id, contact_id="1", start_node=t.start_node
    )
    return Pipeline(t), st


async def test_conversa_real_chega_ao_agendamento(pipe) -> None:
    p, st = pipe

    r1 = await p.rodar_turno(st, "oi, tudo bem?")
    assert r1.texto, "o agente precisa dizer alguma coisa"
    assert not r1.finished

    r2 = await p.rodar_turno(st, "sou o Rafael, queria fazer a barba")
    assert st["collected"].get("nome"), f"não extraiu o nome; coletado={st['collected']}"
    assert st["collected"].get("servico") == "barba"

    # A agenda foi consultada e o agente ofereceu horários que existem.
    agenda = st["collected"].get("agenda") or {}
    assert agenda.get("slots"), "a tool de agenda não rodou"
    labels = [s["label"] for s in agenda["slots"]]
    assert any(lb.split(" às ")[1] in r2.texto for lb in labels), (
        f"o agente ofereceu horário fora do payload.\nresposta: {r2.texto}\nagenda: {labels}"
    )

    r3 = await p.rodar_turno(st, f"pode ser {labels[0]}")
    assert r3.finished, f"não fechou o agendamento; parou em {r3.node_id}"
    assert r3.node_id == "e_agendado"


async def test_pedido_de_humano_e_respeitado_em_qualquer_ponto(pipe) -> None:
    """O escape universal, ponta a ponta, com modelo real."""
    p, st = pipe
    await p.rodar_turno(st, "oi")
    r = await p.rodar_turno(st, "quero falar com uma pessoa de verdade, por favor")
    assert r.handoff is True, f"handoff não detectado; comandos={r.comandos_aceitos}"


async def test_o_agente_nao_se_declara_ia(pipe) -> None:
    """A persona proíbe; o guarda determinístico é o backstop."""
    p, st = pipe
    r = await p.rodar_turno(st, "você é um robô? é uma inteligência artificial?")
    assert "sou uma ia" not in r.texto.lower()
    assert "assistente virtual" not in r.texto.lower()
