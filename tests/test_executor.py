"""O executor — a parte do runtime que não tem LLM nenhum.

Todo teste aqui roda sem rede e sem modelo. Se algum dia um destes precisar
de uma chave de API, a fronteira foi violada.
"""

from __future__ import annotations

import pytest
from zoi_routine import parse_routine

from zoi_agno.executor import (
    advance,
    collect_group_satisfied,
    is_filled,
    missing_required,
    normalize_value,
    resolve_path,
    route_decide,
)
from zoi_agno.executor.values import MISSING
from zoi_agno.state import new_session_state
from zoi_agno.tenants import load_tenant

from .conftest import FIXTURES_TENANTS


@pytest.fixture
def demo():
    t = load_tenant("t_demo", base_dir=FIXTURES_TENANTS)
    st = new_session_state(
        thread_id="x", tenant_id="t_demo", contact_id="1", start_node=t.start_node
    )
    return t.routine, st


# --------------------------------------------------------------------------
# comparação de valores
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("entrada", "esperado"),
    [
        (True, "sim"),
        (False, "nao"),
        ("Sim", "sim"),
        ("NÃO", "nao"),
        ("não", "nao"),
        ("Corte", "corte"),
        ("São Paulo", "sao paulo"),
        (None, ""),
        ("verdadeiro", "sim"),
    ],
)
def test_normalizacao_de_valor(entrada, esperado) -> None:
    assert normalize_value(entrada) == esperado


def test_normalizacao_funciona_com_diacritico_nao_portugues() -> None:
    """O mapa de acentos hardcoded quebrava em maiúscula e em alemão."""
    assert normalize_value("Grün") == "grun"
    assert normalize_value("CAFÉ") == "cafe"


def test_resolve_caminho_aninhado() -> None:
    d = {"busca": {"total": 3, "itens": {"primeiro": "AP-001"}}}
    assert resolve_path(d, "busca.total") == 3
    assert resolve_path(d, "busca.itens.primeiro") == "AP-001"
    assert resolve_path(d, "busca.fantasma") is MISSING
    assert resolve_path(d, "") is MISSING


def test_slot_vazio_nao_conta_como_preenchido() -> None:
    """O husk que sobra de um set_slot rejeitado não pode abrir branch."""
    assert is_filled("Ana") is True
    assert is_filled(0) is True, "zero é um valor legítimo"
    assert is_filled(False) is True
    assert is_filled(None) is False
    assert is_filled("") is False
    assert is_filled("   ") is False
    assert is_filled(MISSING) is False


# --------------------------------------------------------------------------
# decide
# --------------------------------------------------------------------------


def _decide(branches_yaml: str, *, collected=None, sinal=None):
    routine = parse_routine(
        "routine_name: t_dec\nversion: 1\n"
        "slots:\n  campo: { type: string }\n"
        "main:\n  start: d_um\n  nodes:\n"
        f"    d_um:\n      type: decide\n      branches:\n{branches_yaml}"
        "      else: e_outro\n"
        '    e_um:\n      type: end\n      farewell: "a"\n'
        '    e_dois:\n      type: end\n      farewell: "b"\n'
        '    e_outro:\n      type: end\n      farewell: "c"\n'
    )
    st = {"collected": collected or {}, "last_signal": sinal}
    return route_decide(routine.main.nodes["d_um"], st)


def test_sinal_ganha_de_campo() -> None:
    """Empate real: sinal decide."""
    assert (
        _decide(
            "        - { on_field: campo, on_value: x, next: e_um }\n"
            "        - { on_signal: escolheu, next: e_dois }\n",
            collected={"campo": "x"},
            sinal="escolheu",
        )
        == "e_dois"
    )


def test_campo_ganha_do_else() -> None:
    assert (
        _decide(
            "        - { on_field: campo, on_value: x, next: e_um }\n", collected={"campo": "x"}
        )
        == "e_um"
    )


def test_sem_branch_aplicavel_cai_no_else() -> None:
    assert (
        _decide(
            "        - { on_field: campo, on_value: x, next: e_um }\n", collected={"campo": "y"}
        )
        == "e_outro"
    )


def test_branch_por_presenca_exige_valor_nao_vazio() -> None:
    """Bug real: campo=None roteava lead qualificado para o re-engajamento."""
    b = "        - { on_field: campo, next: e_um }\n"
    assert _decide(b, collected={"campo": "qualquer coisa"}) == "e_um"
    assert _decide(b, collected={"campo": None}) == "e_outro"
    assert _decide(b, collected={"campo": "  "}) == "e_outro"
    assert _decide(b, collected={}) == "e_outro"


def test_comparacao_de_valor_ignora_acento_e_caixa() -> None:
    assert (
        _decide(
            "        - { on_field: campo, on_value: nao, next: e_um }\n",
            collected={"campo": "NÃO"},
        )
        == "e_um"
    )


# --------------------------------------------------------------------------
# collect_group
# --------------------------------------------------------------------------


def test_grupo_espera_os_obrigatorios(demo) -> None:
    routine, st = demo
    r = advance(routine, st)
    assert r.node_id == "c_abertura"
    assert r.moved is False
    assert set(missing_required(routine.main.nodes["c_abertura"], {}, routine)) == {
        "nome",
        "servico",
    }


def test_grupo_avanca_quando_completo(demo) -> None:
    routine, st = demo
    st["collected"] = {"nome": "Ana", "servico": "corte"}
    r = advance(routine, st)
    assert r.pending_tool is not None and r.pending_tool.ref == "agenda_livre"
    assert r.moved is True


def test_escape_por_max_turns_e_deterministico(demo) -> None:
    """O lead que não quer responder não fica preso.

    Sai do grupo mesmo com os obrigatórios vazios, pelo destino declarado em
    ``on_max_turns`` — que na fixture é o nó de busca de agenda.
    """
    routine, st = demo
    st["turns_in_node"] = 3  # == max_turns do c_abertura
    r = advance(routine, st)
    assert st["collected"] == {}, "escapou sem preencher nada, que é o ponto"
    assert r.node_id == "t_agenda"
    assert r.moved is True


def test_politica_de_saida_any(demo) -> None:
    routine, _ = demo
    node = routine.main.nodes["c_abertura"]
    node.exit_policy = "any"
    assert collect_group_satisfied(node, {"nome": "Ana"}, routine) is True
    assert collect_group_satisfied(node, {}, routine) is False


def test_politica_de_saida_n_of_m(demo) -> None:
    routine, _ = demo
    node = routine.main.nodes["c_abertura"]
    node.exit_policy = "n_of_m"
    node.exit_n = 2
    assert collect_group_satisfied(node, {"nome": "Ana"}, routine) is False
    assert collect_group_satisfied(node, {"nome": "Ana", "servico": "corte"}, routine) is True


# --------------------------------------------------------------------------
# freetalk / tool / end
# --------------------------------------------------------------------------


def test_freetalk_segura_o_cursor_enquanto_conversa(demo) -> None:
    routine, st = demo
    st["current_node"] = "ft_escolhe"
    r = advance(routine, st)
    assert r.node_id == "ft_escolhe" and r.moved is False


def test_freetalk_estoura_e_vai_para_exit_on_timeout(demo) -> None:
    routine, st = demo
    st["current_node"] = "ft_escolhe"
    st["_freetalk_turn_count"] = {"ft_escolhe": 4}  # max_turns do nó
    r = advance(routine, st)
    assert r.node_id == "e_nutricao"
    assert r.finished is True


def test_sinal_roteia_e_e_consumido(demo) -> None:
    routine, st = demo
    st["current_node"] = "d_escolha"
    st["last_signal"] = "escolheu"
    r = advance(routine, st)
    assert r.node_id == "e_agendado" and r.finished is True
    assert st["last_signal"] is None, "o sinal é consumido pela decisão que destrancou"


def test_end_rende_a_despedida_autorada(demo) -> None:
    routine, st = demo
    st["current_node"] = "e_agendado"
    r = advance(routine, st)
    assert r.finished is True
    assert r.say_templates and "Fechado" in r.say_templates[0]


def test_tool_e_marcada_pendente_nao_executada(demo) -> None:
    """O executor não chama nada — quem executa é o pipeline."""
    routine, st = demo
    st["current_node"] = "t_agenda"
    r = advance(routine, st)
    assert r.pending_tool is not None
    assert r.pending_tool.output_to == "agenda"
    assert r.node_id == "t_agenda", "o cursor só sai depois do resultado"


# --------------------------------------------------------------------------
# wait / ciclo
# --------------------------------------------------------------------------


def test_wait_estaciona_a_conversa() -> None:
    """Não avança nem levanta: registra que a conversa está esperando."""
    routine = parse_routine(
        "routine_name: t_wait\nversion: 1\nslots:\n  campo: { type: string }\n"
        "main:\n  start: w_um\n  nodes:\n"
        "    w_um:\n      type: wait\n      mode: user\n      timeout: PT1H\n"
        "      on_timeout: e_fim\n      next: e_fim\n"
        '    e_fim:\n      type: end\n      farewell: "fim"\n'
    )
    st = new_session_state(thread_id="x", tenant_id="t", contact_id="1", start_node="w_um")
    r = advance(routine, st)
    assert r.waiting is not None, "o executor precisa sinalizar a espera"
    assert r.waiting.mode == "user"
    assert st["_waiting"] is True
    assert r.finished is False
    assert st["current_node"] == "w_um", "o cursor não se move ao estacionar"


def test_ciclo_intra_turno_devolve_a_palavra_ao_lead() -> None:
    """Ciclo que só fecha em runtime passa pelo validador e chegaria aqui.

    Caso real (zoi_sdr): um freetalk estoura ``max_turns``, o
    ``exit_on_timeout`` cai num decide cujo guard exige um slot derivado que
    ainda não existe, e o ``else`` volta ao mesmo freetalk. Sem a trava, o
    turno gira até o teto de saltos em vez de deixar o lead falar.
    """
    routine = parse_routine(
        "routine_name: t_ciclo\nversion: 1\nslots:\n  campo: { type: string }\n"
        "main:\n  start: d_um\n  nodes:\n"
        "    d_um:\n      type: decide\n      branches:\n"
        "        - { on_field: campo, next: d_dois }\n      else: d_dois\n"
        "    d_dois:\n      type: decide\n      branches:\n"
        "        - { on_field: campo, next: d_um }\n      else: d_um\n"
        '    e_fim:\n      type: end\n      role: success\n      farewell: "fim"\n'
    )
    st = new_session_state(thread_id="x", tenant_id="t", contact_id="1", start_node="d_um")
    r = advance(routine, st, max_hops=8)
    assert "ciclo intra-turno" in r.reason
    assert r.finished is False


def test_max_hops_e_a_rede_de_ultima_instancia() -> None:
    """A trava de ciclo pega o caso comum; ``max_hops`` cobre o resto.

    Uma cadeia longa de nós distintos não é ciclo — nenhum id se repete — mas
    também não deve rodar sem teto.
    """
    nos = "".join(
        f"    d_{i:03d}:\n      type: decide\n      branches:\n"
        f"        - {{ on_field: campo, next: d_{i + 1:03d} }}\n      else: d_{i + 1:03d}\n"
        for i in range(40)
    )
    routine = parse_routine(
        "routine_name: t_longo\nversion: 1\nslots:\n  campo: { type: string }\n"
        "main:\n  start: d_000\n  nodes:\n"
        + nos
        + '    d_040:\n      type: end\n      role: success\n      farewell: "fim"\n'
    )
    st = new_session_state(thread_id="x", tenant_id="t", contact_id="1", start_node="d_000")
    r = advance(routine, st, max_hops=8)
    assert "max_hops" in r.reason


# --------------------------------------------------------------------------
# sub-rotina
# --------------------------------------------------------------------------


SUB_YAML = """routine_name: t_sub
version: 1
slots:
  dado: { type: string, required: true }
main:
  start: ca_um
  nodes:
    ca_um:
      type: call_subroutine
      ref: sub_a
      next: e_fim
    e_fim:
      type: end
      role: success
      farewell: "acabou o pai"
sub_routines:
  sub_a:
    start: c_sub
    nodes:
      c_sub:
        type: collect_group
        group_name: g
        exit_policy: all
        max_turns: 3
        fields: [dado]
        next: e_sub
      e_sub:
        type: end
        role: success
        farewell: "acabou o sub"
"""


def test_entra_na_sub_rotina_e_empilha_o_retorno() -> None:
    routine = parse_routine(SUB_YAML)
    st = new_session_state(thread_id="x", tenant_id="t", contact_id="1", start_node="ca_um")
    r = advance(routine, st)
    assert r.entered_subflow == "sub_a"
    assert st["scope"] == "sub_a"
    assert st["current_node"] == "c_sub"
    assert st["subflow_stack"][0]["return_to"] == "e_fim"


def test_fim_da_sub_rotina_volta_ao_pai() -> None:
    routine = parse_routine(SUB_YAML)
    st = new_session_state(thread_id="x", tenant_id="t", contact_id="1", start_node="ca_um")
    advance(routine, st)  # entra
    st["collected"]["dado"] = "preenchido"
    r = advance(routine, st)  # completa o sub e desempilha
    assert st["subflow_stack"] == []
    assert st["scope"] == "main"
    assert r.finished is True, "o pai também terminou"
    assert any("acabou o pai" in t for t in r.say_templates)
