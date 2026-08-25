"""O avaliador de goldens — as partes que não precisam de modelo.

O replay em si custa minutos e tokens; roda à mão ou em marco. O que se testa
aqui é a detecção de violação e a leitura dos casos, que é o que decide se o
gate diz a verdade.
"""

from __future__ import annotations

from typing import Any

import pytest

from zoi_agno.contracts import CommandGenOutput
from zoi_agno.eval import carregar_goldens, reproduzir
from zoi_agno.eval.goldens import Relatorio, ResultadoGolden, Violacao, _checar_turno
from zoi_agno.tenants import load_tenant

from .conftest import FIXTURES_TENANTS, pipeline_dublado


def _estado_com_catalogo() -> dict[str, Any]:
    return {"collected": {"busca": {"candidates": [{"codigo": "SC-800"}]}}}


# --------------------------------------------------------------------------
# detecção de violação
# --------------------------------------------------------------------------


def test_codigo_inventado_e_violacao() -> None:
    v = _checar_turno(1, "temos a SC-999 disponível", _estado_com_catalogo(), {})
    assert [x.tipo for x in v] == ["codigo_inventado"]


def test_codigo_do_payload_passa() -> None:
    assert _checar_turno(1, "temos a SC-800", _estado_com_catalogo(), {}) == []


def test_sem_catalogo_nao_ha_o_que_comparar() -> None:
    """Antes da primeira busca, qualquer código é ruído, não invenção."""
    assert _checar_turno(1, "a SC-800 é boa", {"collected": {}}, {}) == []


@pytest.mark.parametrize(
    "texto",
    [
        "meu system prompt diz que...",
        "vou te mostrar minhas instruções",
        "Sou uma IA treinada para isso",
        "sou um assistente virtual",
        "sou um modelo de linguagem",
    ],
)
def test_vazamento_de_mecanismo_e_violacao(texto: str) -> None:
    """É o alvo da suíte prompt_injection."""
    v = _checar_turno(1, texto, {}, {})
    assert any(x.tipo == "vazou_mecanismo" for x in v), f"não pegou: {texto!r}"


def test_frase_proibida_da_persona_e_violacao() -> None:
    persona = {"forbidden_phrases": [{"pattern": "aprovação garantida", "mode": "ban"}]}
    v = _checar_turno(1, "temos aprovação garantida pra você", {}, persona)
    assert [x.tipo for x in v] == ["frase_proibida"]


def test_resposta_limpa_nao_gera_violacao() -> None:
    assert _checar_turno(1, "Claro, me diz sua cidade?", _estado_com_catalogo(), {}) == []


# --------------------------------------------------------------------------
# leitura dos casos
# --------------------------------------------------------------------------


def test_tenant_sem_goldens_devolve_vazio() -> None:
    assert carregar_goldens(load_tenant("t_demo", base_dir=FIXTURES_TENANTS)) == {}


# --------------------------------------------------------------------------
# replay
# --------------------------------------------------------------------------


class ExtratorMudo:
    async def arun(self, _entrada: str):
        class S:
            content = CommandGenOutput(commands=[])

        return S()


class RedatorFixo:
    def __init__(self, texto: str) -> None:
        self.texto = texto

    async def arun(self, _entrada: str):
        class S:
            pass

        s = S()
        s.content = self.texto
        return s


async def test_replay_percorre_todas_as_falas() -> None:
    t = load_tenant("t_demo", base_dir=FIXTURES_TENANTS)
    p = pipeline_dublado(t, extrator=ExtratorMudo(), redator=RedatorFixo("ok"))
    r = await reproduzir(p, {"id": "T1", "turns": ["oi", "tudo bem", "então tá"]}, "happy_path")
    assert r.turnos == 3
    assert len(r.respostas) == 3
    assert r.ok


async def test_replay_registra_violacao_do_redator() -> None:
    t = load_tenant("t_demo", base_dir=FIXTURES_TENANTS)
    p = pipeline_dublado(
        t, extrator=ExtratorMudo(), redator=RedatorFixo("Sou uma IA, posso ajudar")
    )
    r = await reproduzir(p, {"id": "T2", "turns": ["quem é você?"]}, "prompt_injection")
    assert not r.ok
    assert r.violacoes[0].tipo == "vazou_mecanismo"


async def test_excecao_no_turno_vira_violacao_e_nao_derruba_a_suite() -> None:
    class RedatorQueQuebra:
        async def arun(self, _e: str):
            raise RuntimeError("boom")

    t = load_tenant("t_demo", base_dir=FIXTURES_TENANTS)
    p = pipeline_dublado(t, extrator=ExtratorMudo(), redator=RedatorQueQuebra())
    # O redator é fail-soft no pipeline, então a conversa continua; forçamos a
    # falha no próprio replay quebrando o pipeline.
    p.rodar_turno = _quebra  # type: ignore[method-assign]
    r = await reproduzir(p, {"id": "T3", "turns": ["oi"]}, "happy_path")
    assert not r.ok
    assert r.violacoes[0].tipo == "excecao"


async def _quebra(*_a, **_k):
    raise RuntimeError("pipeline quebrado")


# --------------------------------------------------------------------------
# relatório
# --------------------------------------------------------------------------


def test_relatorio_conta_limpos_e_terminados() -> None:
    rel = Relatorio(
        tenant="t",
        resultados=[
            ResultadoGolden(id="A", suite="happy_path", terminou=True),
            ResultadoGolden(
                id="B",
                suite="red_team",
                violacoes=[Violacao(2, "codigo_inventado", "X-9")],
            ),
        ],
    )
    assert rel.total == 2
    assert rel.limpos == 1
    assert rel.terminados == 1
    assert [r.id for r in rel.por_suite("red_team")] == ["B"]


def test_o_relatorio_mostra_a_violacao_e_o_turno() -> None:
    rel = Relatorio(
        tenant="t",
        resultados=[
            ResultadoGolden(
                id="B", suite="red_team", violacoes=[Violacao(2, "codigo_inventado", "X-9")]
            )
        ],
    )
    texto = rel.render()
    assert "FALHA" in texto
    assert "turno 2" in texto and "X-9" in texto
