"""O pipeline de turno, com cérebros falsos.

Nenhum teste aqui toca a rede. Os dois cérebros são substituídos por dublês
que devolvem exatamente o que queremos exercitar — é assim que se testa a
costura sem pagar latência nem token, e sem que o resultado dependa do humor
do modelo.

A conversa com LLM de verdade vive em ``test_pipeline_live.py``, marcada para
rodar só quando há chave.
"""

from __future__ import annotations

from typing import Any

import pytest

from zoi_agno.contracts import CommandGenOutput
from zoi_agno.pipeline import Pipeline
from zoi_agno.state import new_session_state
from zoi_agno.tenants import load_tenant

from .conftest import FIXTURES_TENANTS


class _Saida:
    def __init__(self, content: Any) -> None:
        self.content = content


class ExtratorFalso:
    """Devolve lotes de comandos pré-programados, um por turno."""

    def __init__(self, lotes: list[list[dict[str, Any]]]) -> None:
        self.lotes = list(lotes)
        self.entradas: list[str] = []

    async def arun(self, entrada: str) -> _Saida:
        self.entradas.append(entrada)
        lote = self.lotes.pop(0) if self.lotes else []
        return _Saida(CommandGenOutput.model_validate({"commands": lote}))


class RedatorFalso:
    def __init__(self, texto: str = "resposta do agente") -> None:
        self.texto = texto
        self.entradas: list[str] = []

    async def arun(self, entrada: str) -> _Saida:
        self.entradas.append(entrada)
        return _Saida(self.texto)


class CerebroQueQuebra:
    async def arun(self, entrada: str) -> _Saida:
        raise RuntimeError("provedor fora do ar")


@pytest.fixture
def pipe():
    t = load_tenant("t_demo", base_dir=FIXTURES_TENANTS)
    p = Pipeline(t)
    st = new_session_state(
        thread_id="tg:1", tenant_id=t.tenant_id, contact_id="1", start_node=t.start_node
    )
    return p, st


def _slot(nome: str, valor: Any, **extra: Any) -> dict[str, Any]:
    return {"kind": "set_slot", "payload": {"slot": nome, "value": valor}, **extra}


# --------------------------------------------------------------------------


async def test_turno_completo_move_o_cursor(pipe) -> None:
    p, st = pipe
    p.extrator = ExtratorFalso([[_slot("nome", "Ana"), _slot("servico", "corte")]])
    p.redator = RedatorFalso("Fechado, Ana!")

    r = await p.rodar_turno(st, "sou a Ana, quero um corte")

    assert st["collected"]["nome"] == "Ana"
    assert st["collected"]["servico"] == "corte"
    assert r.texto == "Fechado, Ana!"
    assert r.node_id != "c_abertura", "com o grupo completo, o cursor tinha que sair"


async def test_multi_extracao_num_turno_so(pipe) -> None:
    """Um slot por turno é o que faz o agente parecer formulário."""
    p, st = pipe
    p.extrator = ExtratorFalso([[_slot("nome", "Ana"), _slot("servico", "barba")]])
    p.redator = RedatorFalso()
    await p.rodar_turno(st, "Ana, barba")
    assert len(st["collected"]) >= 2


async def test_tool_e_executada_e_o_payload_vira_estado(pipe) -> None:
    p, st = pipe
    p.extrator = ExtratorFalso([[_slot("nome", "Ana"), _slot("servico", "corte")]])
    p.redator = RedatorFalso()

    await p.rodar_turno(st, "Ana, corte")

    agenda = st["collected"].get("agenda")
    assert isinstance(agenda, dict) and agenda.get("slots"), "a agenda devia ter sido consultada"
    assert st["tool_call_log"][0]["ref"] == "agenda_livre"


def test_argumentos_da_tool_sao_resolvidos_do_estado(pipe) -> None:
    from zoi_agno.pipeline import _render_args

    st = {"collected": {"servico": "barba"}}
    assert _render_args({"servico": "{{ lead.servico }}", "n": 3}, st) == {
        "servico": "barba",
        "n": 3,
    }


def test_argumento_de_slot_vazio_e_omitido(pipe) -> None:
    """Mandar ``None`` para a tool é pior que não mandar o filtro."""
    from zoi_agno.pipeline import _render_args

    assert _render_args({"servico": "{{ lead.servico }}"}, {"collected": {}}) == {}


async def test_enforcement_barra_slot_fora_de_escopo(pipe) -> None:
    """O pipeline não aplica o que a fiscalização recusou."""
    p, st = pipe
    p.extrator = ExtratorFalso([[_slot("cpf_do_avo", "123")]])
    p.redator = RedatorFalso()

    r = await p.rodar_turno(st, "meu avô tem cpf")

    assert "cpf_do_avo" not in st["collected"]
    assert any(x["code"] == "slot_out_of_scope" for x in r.rejeicoes)


async def test_confianca_baixa_vira_confirmacao_pendente(pipe) -> None:
    p, st = pipe
    p.extrator = ExtratorFalso([[_slot("nome", "Ana", confidence=0.3)]])
    p.redator = RedatorFalso()

    await p.rodar_turno(st, "acho que é Ana")

    assert "nome" not in st["collected"], "valor incerto não vira verdade"
    assert st["pending_confirmations"].get("nome") == "Ana"


async def test_handoff_e_sinalizado_ao_canal(pipe) -> None:
    p, st = pipe
    p.extrator = ExtratorFalso([[{"kind": "handoff_human", "payload": {"reason": "quer humano"}}]])
    p.redator = RedatorFalso()

    r = await p.rodar_turno(st, "quero falar com uma pessoa")

    assert r.handoff is True
    assert st["_handoff_reason"] == "quer humano"


async def test_rejeicoes_nao_vazam_entre_turnos(pipe) -> None:
    """Bug real do v4: as rejeições acumulavam até o fim da conversa."""
    p, st = pipe
    p.extrator = ExtratorFalso([[_slot("cpf_do_avo", "x")], [_slot("nome", "Ana")]])
    p.redator = RedatorFalso()

    await p.rodar_turno(st, "primeiro turno")
    assert st["enforcement_rejections"]
    await p.rodar_turno(st, "segundo turno")
    assert st["enforcement_rejections"] == []


async def test_extrator_fora_do_ar_nao_derruba_o_turno(pipe) -> None:
    """Sem extração o turno perde informação, mas o lead recebe resposta."""
    p, st = pipe
    p.extrator = CerebroQueQuebra()
    p.redator = RedatorFalso("desculpa, pode repetir?")

    r = await p.rodar_turno(st, "sou a Ana")

    assert r.texto == "desculpa, pode repetir?"
    assert st["collected"] == {}


async def test_redator_fora_do_ar_cai_no_texto_do_roteiro(pipe) -> None:
    """Melhor a cópia autorada que o silêncio."""
    p, st = pipe
    st["current_node"] = "e_agendado"
    p.extrator = ExtratorFalso([[]])
    p.redator = CerebroQueQuebra()

    r = await p.rodar_turno(st, "ok")

    assert "Fechado" in r.texto, "devia ter usado o farewell do nó end"


async def test_guarda_derruba_codigo_inventado(pipe) -> None:
    """O redator citou um item que nenhum payload sustenta."""
    p, st = pipe
    st["current_node"] = "e_agendado"
    st["collected"]["busca"] = {"candidates": [{"codigo": "AP-001"}]}
    p.extrator = ExtratorFalso([[]])
    p.redator = RedatorFalso("temos a AP-999 disponível")

    r = await p.rodar_turno(st, "tem outro?")

    assert "AP-999" not in r.texto
    assert any(x["code"] == "codigo_inventado" for x in st["enforcement_rejections"])


async def test_o_extrator_recebe_o_contexto_do_no_atual(pipe) -> None:
    """Dar o grafo inteiro convida o modelo a preencher etapas futuras."""
    p, st = pipe
    p.extrator = ExtratorFalso([[]])
    p.redator = RedatorFalso()

    await p.rodar_turno(st, "oi")

    entrada = p.extrator.entradas[0]
    assert "abertura" in entrada, "devia citar o grupo em coleta"
    assert "ft_escolhe" not in entrada, "não devia expor nós futuros"


async def test_o_redator_recebe_o_contexto_factual(pipe) -> None:
    p, st = pipe
    st["collected"]["nome"] = "Ana"
    p.extrator = ExtratorFalso([[]])
    p.redator = RedatorFalso()

    await p.rodar_turno(st, "oi")

    entrada = p.redator.entradas[0]
    assert "CONTEXTO FACTUAL" in entrada
    assert "Ana" in entrada


async def test_conversa_de_tres_turnos_chega_ao_fim(pipe) -> None:
    """A costura inteira, sem rede: coleta → tool → sinal → end."""
    p, st = pipe
    p.extrator = ExtratorFalso(
        [
            [_slot("nome", "Ana"), _slot("servico", "corte")],
            [
                _slot("horario", "terça às 11:00"),
                {"kind": "signal", "payload": {"name": "escolheu", "value": True}},
            ],
        ]
    )
    p.redator = RedatorFalso()

    r1 = await p.rodar_turno(st, "Ana, corte")
    assert r1.finished is False

    r2 = await p.rodar_turno(st, "terça às 11")
    assert r2.finished is True
    assert r2.node_id == "e_agendado"
