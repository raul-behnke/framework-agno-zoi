"""As partes determinísticas dos cinco cérebros.

Prompt e qualidade de texto não se testam aqui — isso é dos goldens. O que se
testa é a lógica que decide **quando** cada cérebro roda e **o que** ele
recebe, porque é ela que determina o custo por turno e o que o modelo pode
inventar.
"""

from __future__ import annotations

import pytest

from zoi_agno.brains import composer, critic, extractor, planner, tone
from zoi_agno.executor import current_node
from zoi_agno.state import new_session_state
from zoi_agno.tenants import load_tenant

from .conftest import FIXTURES_TENANTS


@pytest.fixture
def tenant():
    return load_tenant("t_demo", base_dir=FIXTURES_TENANTS)


@pytest.fixture
def estado_demo(tenant):
    return new_session_state(
        thread_id="x", tenant_id="t_demo", contact_id="1", start_node=tenant.start_node
    )


# --------------------------------------------------------------------------
# crítico de decisão — o portão é o que controla o custo
# --------------------------------------------------------------------------


def test_turno_comum_nao_aciona_o_critico(estado_demo) -> None:
    """Sem decisão irreversível, o crítico não custa nada."""
    aceitos = [{"kind": "set_slot", "payload": {"slot": "nome", "value": "Ana"}}]
    assert critic.avaliar_portao(aceitos, estado_demo).acionado is False


@pytest.mark.parametrize("kind", ["handoff_human", "finish_flow"])
def test_decisao_irreversivel_aciona_o_critico(kind: str, estado_demo) -> None:
    portao = critic.avaliar_portao([{"kind": kind, "payload": {}}], estado_demo)
    assert portao.acionado is True
    assert portao.decisao == kind


def test_apresentacao_de_catalogo_aciona_o_critico(estado_demo) -> None:
    estado_demo["_apresentou_catalogo"] = True
    assert critic.avaliar_portao([], estado_demo).acionado is True


def test_o_critico_recebe_a_conversa_recente(estado_demo) -> None:
    estado_demo["messages_tail"] = [
        {"role": "user", "content": "quero falar com alguém"},
        {"role": "assistant", "content": "claro"},
    ]
    estado_demo["collected"]["nome"] = "Ana"
    entrada = critic.montar_entrada("handoff_human", estado_demo, "quero falar com alguém")
    assert "handoff_human" in entrada
    assert "quero falar com alguém" in entrada
    assert "Ana" in entrada


async def test_critico_quebrado_aprova(estado_demo) -> None:
    """Fail-soft: a conversa nunca trava esperando o crítico."""

    class Quebrado:
        async def arun(self, _entrada):
            raise RuntimeError("fora do ar")

    v = await critic.julgar(Quebrado(), "handoff_human", estado_demo, "oi")
    assert v.aprovado is True


# --------------------------------------------------------------------------
# crítico de tom — o modo controla quantas chamadas por turno
# --------------------------------------------------------------------------


def test_modo_always_roda_sempre() -> None:
    assert tone.ConfigTom(modo="always").deve_rodar() is True


def test_modo_off_nunca_roda() -> None:
    assert tone.ConfigTom(modo="off").deve_rodar() is False


def test_modo_conditional_amostra() -> None:
    cfg = tone.ConfigTom(modo="conditional", taxa_amostragem=0.10)
    assert cfg.deve_rodar(sorteio=0.05) is True
    assert cfg.deve_rodar(sorteio=0.50) is False


def test_o_critico_de_tom_recebe_a_voz_e_as_proibicoes(tenant) -> None:
    entrada = tone.montar_entrada("Prezado cliente, segue abaixo.", tenant.persona)
    assert "VOZ ESPERADA" in entrada
    assert "rapidinho" in entrada, "as frases proibidas da persona devem ir junto"
    assert "Prezado cliente" in entrada


async def test_rascunho_vazio_nao_gasta_chamada() -> None:
    class NuncaChamado:
        async def arun(self, _entrada):
            raise AssertionError("não deveria ser chamado")

    v = await tone.revisar(NuncaChamado(), "   ", {})
    assert v.aprovado is True


async def test_critico_de_tom_quebrado_aprova() -> None:
    class Quebrado:
        async def arun(self, _entrada):
            raise RuntimeError("fora do ar")

    v = await tone.revisar(Quebrado(), "oi", {})
    assert v.aprovado is True


# --------------------------------------------------------------------------
# planner — não move o cursor; conta desvio
# --------------------------------------------------------------------------


def test_planner_so_ve_nos_do_escopo_ativo(tenant, estado_demo) -> None:
    """A lista fechada é o que impede o plano de citar nó inventado."""
    nos = planner.nos_alcancaveis(tenant.routine, estado_demo)
    assert "c_abertura" in nos
    assert all(n in tenant.routine.main.nodes for n in nos)


def test_a_entrada_do_planner_traz_o_objetivo_do_fluxo(tenant, estado_demo) -> None:
    entrada = planner.montar_entrada(tenant.routine, estado_demo, "oi", tenant.routine.flow_goal)
    assert "OBJETIVO DO FLUXO" in entrada
    assert "c_abertura" in entrada


def test_intencao_com_nome_de_tipo_de_no_e_coagida() -> None:
    """O modelo escreve `collect_group` onde o schema espera `ask_group`."""
    bruto = {"passos": [{"intencao": "collect_group", "alvo": "c_um"}]}
    assert planner.coagir_intencoes(bruto)["passos"][0]["intencao"] == "ask_group"


def test_drift_zera_quando_o_cursor_segue_o_plano(estado_demo) -> None:
    plano = planner.Plano(passos=[planner.PassoDoPlano(intencao="ask_group", alvo="c_abertura")])
    estado_demo["_drift_streak"] = 3
    estado_demo["current_node"] = "c_abertura"
    assert planner.atualizar_drift(estado_demo, plano) == 0


def test_drift_acumula_quando_a_conversa_foge_do_plano(estado_demo) -> None:
    plano = planner.Plano(passos=[planner.PassoDoPlano(intencao="ask_group", alvo="c_abertura")])
    estado_demo["current_node"] = "ft_escolhe"
    assert planner.atualizar_drift(estado_demo, plano) == 1
    assert planner.atualizar_drift(estado_demo, plano) == 2


def test_sem_plano_o_drift_nao_se_mexe(estado_demo) -> None:
    estado_demo["_drift_streak"] = 2
    assert planner.atualizar_drift(estado_demo, None) == 2


async def test_plano_com_no_inventado_e_descartado(tenant, estado_demo) -> None:
    """Passo para nó inexistente incha o prompt e antecipa ramo morto."""

    class PlanejadorAlucinado:
        async def arun(self, _entrada):
            class S:
                content = planner.Plano(
                    passos=[planner.PassoDoPlano(intencao="end", alvo="n_fantasma")]
                )

            return S()

    plano = await planner.planejar(PlanejadorAlucinado(), tenant.routine, estado_demo, "oi")
    assert plano is None, "sem passo válido, o turno segue em modo reativo"


# --------------------------------------------------------------------------
# extrator e redator — o que cada um enxerga
# --------------------------------------------------------------------------


def test_extrator_ve_os_campos_do_grupo_em_coleta(tenant, estado_demo) -> None:
    node = current_node(tenant.routine, estado_demo)
    entrada = extractor.montar_entrada("oi", node, tenant.routine, {})
    assert "nome" in entrada and "servico" in entrada
    assert "corte" in entrada, "os valores do enum precisam estar visíveis"


def test_extrator_ve_os_sinais_declarados_do_freetalk(tenant, estado_demo) -> None:
    estado_demo["current_node"] = "ft_escolhe"
    node = current_node(tenant.routine, estado_demo)
    entrada = extractor.montar_entrada("quero a de terça", node, tenant.routine, {})
    assert "escolheu" in entrada and "quer_humano" in entrada


def test_extrator_ve_o_que_ja_foi_coletado(tenant, estado_demo) -> None:
    """Para não re-extrair, e para perceber correção do lead."""
    node = current_node(tenant.routine, estado_demo)
    entrada = extractor.montar_entrada("na verdade é Bruno", node, tenant.routine, {"nome": "Ana"})
    assert "Ana" in entrada


def test_grounding_expoe_payload_de_tool_e_esconde_chave_interna(estado_demo) -> None:
    estado_demo["collected"]["nome"] = "Ana"
    estado_demo["collected"]["agenda"] = {"slots": [{"slot_id": "x"}]}
    estado_demo["collected"]["_interno"] = "não deve aparecer"
    g = composer.build_grounding(estado_demo, None)
    assert g["coletado"]["nome"] == "Ana"
    assert "_interno" not in g["coletado"]
    assert g["agenda"]["slots"], "payload de tool precisa ser citável"


def test_a_tarefa_do_redator_vem_da_pergunta_autorada(tenant, estado_demo) -> None:
    node = current_node(tenant.routine, estado_demo)
    entrada = composer.montar_entrada(
        user_msg="oi", node=node, routine=tenant.routine, state=estado_demo
    )
    assert "TAREFA" in entrada
    assert "como é seu nome" in entrada, "a pergunta canônica do YAML deve chegar ao redator"


def test_o_plano_chega_ao_redator_sem_expor_o_mecanismo(estado_demo, tenant) -> None:
    """O redator conduz na direção do plano, mas não fala de nós nem etapas."""
    estado_demo["plan"] = {
        "passos": [
            {"intencao": "ask_group", "alvo": "c_abertura", "porque": "falta o nome"},
            {"intencao": "subflow", "alvo": "t_agenda", "porque": "depois busca horário"},
        ]
    }
    node = current_node(tenant.routine, estado_demo)
    entrada = composer.montar_entrada(
        user_msg="oi", node=node, routine=tenant.routine, state=estado_demo
    )
    assert "PARA ONDE A CONVERSA VAI" in entrada
    assert "falta o nome" in entrada
    assert "c_abertura" not in entrada.split("PARA ONDE A CONVERSA VAI")[1], (
        "id de nó não diz nada ao redator e convida a mencionar o mecanismo"
    )


def test_handoff_muda_a_tarefa_do_redator(estado_demo, tenant) -> None:
    """Bug real: o canal marcava escalada e o texto oferecia opções."""
    estado_demo["_handoff_reason"] = "o lead pediu vendedor"
    node = current_node(tenant.routine, estado_demo)
    entrada = composer.montar_entrada(
        user_msg="quero uma pessoa",
        node=node,
        routine=tenant.routine,
        state=estado_demo,
        handoff=True,
    )
    assert "ESCALA" in entrada
    assert "NÃO ofereça opções" in entrada
