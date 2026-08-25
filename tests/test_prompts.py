"""Os fragmentos de prompt compartilhados.

Cada bloco aqui muda o que o agente pode dizer. Não são enfeites: a hierarquia
de instrução é a fronteira de confiança do canal público, e a proibição sem
catálogo é o que impede o agente de inventar produto quando o objetivo do
fluxo manda "apresentar opções".
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from zoi_agno.brains import composer, extractor
from zoi_agno.executor import current_node
from zoi_agno.prompts import (
    HIERARQUIA_DE_INSTRUCAO,
    contexto_de_fluxo,
    linha_de_hoje,
    montar_grounding,
    render_grounding,
)
from zoi_agno.state import new_session_state
from zoi_agno.tenants import load_tenant

from .conftest import FIXTURES_TENANTS


def _tenant():
    return load_tenant("t_demo", base_dir=FIXTURES_TENANTS)


def _estado(**over):
    st = new_session_state(thread_id="x", tenant_id="t", contact_id="1", start_node="c_abertura")
    st.update(over)
    return st


# --------------------------------------------------------------------------
# hierarquia de instrução — a fronteira de confiança
# --------------------------------------------------------------------------


def test_os_dois_cerebros_que_leem_o_lead_declaram_a_hierarquia() -> None:
    """A mensagem vem de canal público: é dado, não ordem."""
    assert HIERARQUIA_DE_INSTRUCAO in extractor.INSTRUCOES
    assert HIERARQUIA_DE_INSTRUCAO in composer.BASE


def test_a_hierarquia_cobre_injecao_e_sigilo() -> None:
    texto = HIERARQUIA_DE_INSTRUCAO.lower()
    assert "dado" in texto and "ordem" in texto
    assert "não revele" in texto
    assert "prevalecem" in texto


# --------------------------------------------------------------------------
# âncora temporal
# --------------------------------------------------------------------------


def test_a_data_de_hoje_sai_em_portugues() -> None:
    fixa = datetime(2026, 9, 1, 10, 0, tzinfo=ZoneInfo("America/Sao_Paulo"))
    assert linha_de_hoje(fixa) == "Hoje é terça-feira, 01/09/2026."


def test_a_data_chega_aos_dois_prompts() -> None:
    """Sem ela, 'terça que vem' e 'amanhã' viram chute."""
    t = _tenant()
    st = _estado()
    node = current_node(t.routine, st)
    assert "Hoje é" in extractor.montar_entrada("oi", node, t.routine, {})
    assert "Hoje é" in composer.montar_entrada(
        user_msg="oi", node=node, routine=t.routine, state=st
    )


# --------------------------------------------------------------------------
# projeção — cada coisa aparece uma vez
# --------------------------------------------------------------------------


def test_payload_de_tool_nao_conta_como_slot_do_lead() -> None:
    st = _estado()
    st["collected"] = {"nome": "Ana", "agenda": {"slots": []}, "_interno": "x"}
    fluxo = contexto_de_fluxo(st)
    assert fluxo.slots == {"nome": "Ana"}


def test_o_payload_aparece_uma_vez_so_no_prompt_do_redator() -> None:
    """Antes, o mesmo blob de candidatos ia em dois campos do mesmo prompt."""
    t = _tenant()
    st = _estado()
    st["collected"] = {"nome": "Ana", "agenda": {"slots": [{"slot_id": "MARCA-XYZ"}]}}
    entrada = composer.montar_entrada(
        user_msg="oi", node=current_node(t.routine, st), routine=t.routine, state=st
    )
    assert entrada.count("MARCA-XYZ") == 1


def test_confirmacao_pendente_e_visivel_ao_redator() -> None:
    st = _estado(pending_confirmations={"nome": "Ana"})
    fluxo = contexto_de_fluxo(st)
    assert fluxo.confirmacoes_pendentes == {"nome": "Ana"}


# --------------------------------------------------------------------------
# grounding — a proibição que impede invenção
# --------------------------------------------------------------------------


def test_sem_busca_o_redator_e_proibido_de_citar_item() -> None:
    """Contraria a puxada do objetivo do fluxo, que manda 'apresentar opções'."""
    bloco = render_grounding(montar_grounding(_estado()), "redator")
    assert "PROIBIDO" in bloco
    assert "BUSCA AINDA NÃO RODOU" in bloco


def test_com_busca_a_proibicao_some() -> None:
    st = _estado()
    st["collected"] = {"agenda": {"slots": [{"slot_id": "x"}]}}
    bloco = render_grounding(montar_grounding(st), "redator")
    assert "PROIBIDO" not in bloco


def test_busca_que_cedeu_criterio_gera_disclosure() -> None:
    """Oferecer o mais próximo como se fosse o pedido é o que soa desonesto."""
    st = _estado()
    st["collected"] = {
        "busca": {"candidates": [{"codigo": "X-1"}], "relaxed": ["potencia", "preco"]}
    }
    bloco = render_grounding(montar_grounding(st), "redator")
    assert "AMPLIOU" in bloco
    assert "potencia" in bloco and "preco" in bloco
    assert "mais próximo" in bloco


def test_a_proibicao_fica_por_ultimo_no_bloco() -> None:
    """Posição mais saliente: é a última coisa que o modelo lê."""
    st = _estado(enforcement_rejections=[{"code": "slot_out_of_scope", "detail": "x"}])
    bloco = render_grounding(montar_grounding(st), "redator")
    assert bloco.rindex("PROIBIDO") > bloco.index("descartou informação")


def test_cada_papel_ve_so_o_seu_canal() -> None:
    st = _estado(_known_facts=[{"name": "tem_loja", "value": True}])
    st["enforcement_rejections"] = [{"code": "algum_codigo", "detail": "d"}]
    g = montar_grounding(st)
    assert "tem_loja" in render_grounding(g, "redator")
    assert "tem_loja" not in render_grounding(g, "extrator")
    assert "algum_codigo" in render_grounding(g, "extrator")


def test_grounding_de_papel_desconhecido_e_vazio() -> None:
    assert render_grounding(montar_grounding(_estado()), "inexistente") == ""


# --------------------------------------------------------------------------
# nó de escolha — set_slot e signal têm que sair juntos
# --------------------------------------------------------------------------


def test_no_de_escolha_manda_emitir_slot_e_sinal_juntos() -> None:
    """Bug real: o lead escolhia o horário e o agendamento nunca disparava.

    O extrator gravava o slot e esquecia o sinal; o decide seguinte não tinha
    o que consumir e a conversa voltava a perguntar.
    """
    t = _tenant()
    st = _estado(current_node="ft_escolhe")
    entrada = extractor.montar_entrada(
        "quero terça às 11", current_node(t.routine, st), t.routine, {}
    )
    assert "CAPTURA" in entrada
    assert "TAMBÉM o signal" in entrada
    assert "valor EXATO" in entrada


def test_no_de_conversa_sem_slot_nao_recebe_a_diretiva_de_pick() -> None:
    """A diretiva só vale onde há escolha a capturar."""
    t = _tenant()
    st = _estado()
    entrada = extractor.montar_entrada("oi", current_node(t.routine, st), t.routine, {})
    assert "TAMBÉM o signal" not in entrada


def test_relaxed_do_motor_de_catalogo_vira_disclosure() -> None:
    """O motor devolve dicionários descritivos, não nomes de eixo.

    Integração real: uma tool simples pode devolver ``["preco"]`` e o motor
    devolve ``[{"param": "potencia", "from": 1000, "to": 800}]``. Se o
    grounding só entendesse uma das formas, a disclosure de honestidade
    silenciosamente não sairia — exatamente no caso em que ela mais importa.
    """
    st = _estado()
    st["collected"] = {
        "busca": {
            "candidates": [{"codigo": "SC-800"}],
            "relaxed": [
                {"param": "potencia", "from": 1000, "to": 800},
                {"param": "categoria", "from": "scooter", "to": "scooter +1"},
            ],
        }
    }
    bloco = render_grounding(montar_grounding(st), "redator")
    assert "AMPLIOU" in bloco
    assert "potencia (1000 → 800)" in bloco
    assert "categoria" in bloco


def test_relaxed_como_lista_de_strings_tambem_funciona() -> None:
    st = _estado()
    st["collected"] = {"busca": {"candidates": [{"codigo": "X"}], "relaxed": ["preco"]}}
    assert "preco" in render_grounding(montar_grounding(st), "redator")


def test_o_extrator_ve_todos_os_slots_do_fluxo() -> None:
    """O lead responde na ordem dele, não na do roteiro.

    Falha real no sal_imports: o lead disse "sou o Marcos de Bangu, consigo
    falar sim" enquanto o nó ativo coletava só disponibilidade. Sem ver que
    ``nome`` e ``cidade`` existem, o extrator descartava os dois — e o agente
    perguntava o nome de novo nos dois turnos seguintes.
    """
    from zoi_agno.brains.extractor import manifesto_de_slots

    t = _tenant()
    st = _estado()
    node = current_node(t.routine, st)
    m = manifesto_de_slots(t.routine, node)
    # O grupo ativo coleta nome e servico; horario é de outra etapa.
    assert "▶ nome" in m
    assert "▶ servico" in m
    assert "  horario" in m, "slot de etapa futura precisa aparecer, sem marca"
    assert "CAPTURE" in m


def test_o_manifesto_marca_so_o_que_esta_sendo_perguntado() -> None:
    from zoi_agno.brains.extractor import manifesto_de_slots

    t = _tenant()
    st = _estado(current_node="ft_escolhe")
    m = manifesto_de_slots(t.routine, current_node(t.routine, st))
    assert "▶ horario" in m
    assert "▶ nome" not in m


def test_no_de_escolha_recebe_os_ids_das_opcoes() -> None:
    """Sem os ids, a instrução de "use o id exato" é impossível de cumprir.

    A projeção de contexto tira os payloads de tool da visão do extrator, de
    propósito, para não duplicar o blob. Mas num nó de escolha ele PRECISA
    dos ids: o lead escolhia "terça às 9", o extrator gravava o rótulo, a
    rule de agendamento barrava (corretamente) e a conversa ficava pedindo
    confirmação para sempre.
    """
    from zoi_agno.brains.extractor import opcoes_apresentadas

    t = _tenant()
    st = _estado(current_node="ft_escolhe")
    collected = {
        "agenda": {
            "slots": [
                {"slot_id": "corte:2026-09-01T09:00", "label": "terça às 09:00"},
                {"slot_id": "corte:2026-09-01T14:00", "label": "terça às 14:00"},
            ]
        }
    }
    bloco = opcoes_apresentadas(current_node(t.routine, st), collected)
    assert "corte:2026-09-01T09:00" in bloco
    assert "terça às 09:00" in bloco
    assert "EXATO" in bloco


def test_no_sem_escolha_nao_recebe_as_opcoes() -> None:
    """Payload de tool num nó de coleta é ruído: infla o prompt sem uso."""
    from zoi_agno.brains.extractor import opcoes_apresentadas

    t = _tenant()
    st = _estado()  # c_abertura, um collect_group
    collected = {"agenda": {"slots": [{"slot_id": "x", "label": "y"}]}}
    assert opcoes_apresentadas(current_node(t.routine, st), collected) == ""
