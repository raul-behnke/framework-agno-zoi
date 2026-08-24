"""Um teste por rule — o comportamento que ela existe para garantir.

Cada rule do v4 nasceu de um bug de produção documentado no seu docstring.
Estes testes fixam esse comportamento, para que o porte para o Agno não
perca a cicatriz.
"""

from __future__ import annotations

import pytest

from zoi_agno.enforcement.album_scope import AlbumScopeRule
from zoi_agno.enforcement.appointment_slot_scope import AppointmentSlotScopeRule
from zoi_agno.enforcement.branch_gating_slot import BranchGatingSlotRule
from zoi_agno.enforcement.collect_group_exit import CollectGroupExitRule
from zoi_agno.enforcement.confidence import ConfidenceThresholdRule
from zoi_agno.enforcement.cost_cap_v4 import CostCapRule
from zoi_agno.enforcement.finish_flow_graph import FinishFlowGraphRule
from zoi_agno.enforcement.interrogative_user import InterrogativeUserMsgRule
from zoi_agno.enforcement.permission_v4 import DEFAULT_AGENT_ALLOWED_KINDS, PermissionRule
from zoi_agno.enforcement.plan_reach import PlanReachabilityRule
from zoi_agno.enforcement.say_scope import SayScopeRule
from zoi_agno.enforcement.signal_guard import SignalGuardRule
from zoi_agno.enforcement.signal_normalize import SignalNameNormalizeRule
from zoi_agno.enforcement.signal_rate import SignalRateLimitRule
from zoi_agno.enforcement.skip_collect_rate import SkipCollectRateLimitRule
from zoi_agno.enforcement.slot_scope import SlotScopeRule
from zoi_agno.enforcement.slot_validator import SlotValidatorRule
from zoi_agno.enforcement.socializer_scope_v4 import SocializerScopeRule
from zoi_agno.enforcement.subflow_v4 import SubflowRule

from .conftest import cmd, ctx

# --------------------------------------------------------------------------
# slot_scope — contenção anti-fabricação
# --------------------------------------------------------------------------


async def test_slot_scope_aceita_slot_do_fluxo(estado) -> None:
    r = await SlotScopeRule().check(
        cmd("set_slot", {"slot": "nome", "value": "Ana"}), estado, ctx()
    )
    assert r is None


async def test_slot_scope_rejeita_slot_inventado(estado) -> None:
    r = await SlotScopeRule().check(
        cmd("set_slot", {"slot": "cpf_do_avo", "value": "x"}), estado, ctx()
    )
    assert r is not None and r.code == "slot_out_of_scope"


# --------------------------------------------------------------------------
# branch_gating_slot — anti-misroute
# --------------------------------------------------------------------------


async def test_branch_gating_bloqueia_slot_setado_fora_do_no_dono(estado) -> None:
    """O extrator alucinava `tem_modelo` na abertura e desviava o fluxo todo."""
    estado["current_node"] = "c_abertura"
    c = ctx(business={"branch_gating_slots": {"nome": "c_abordagem"}})
    r = await BranchGatingSlotRule().check(
        cmd("set_slot", {"slot": "nome", "value": "sim"}), estado, c
    )
    assert r is not None and r.code == "branch_slot_premature"


async def test_branch_gating_permite_no_no_dono(estado) -> None:
    estado["current_node"] = "c_abordagem"
    c = ctx(business={"branch_gating_slots": {"nome": "c_abordagem"}})
    assert (
        await BranchGatingSlotRule().check(
            cmd("set_slot", {"slot": "nome", "value": "Ana"}), estado, c
        )
        is None
    )


async def test_branch_gating_e_opt_in_por_tenant(estado) -> None:
    """Sem o mapa no business.yaml, a rule é no-op."""
    assert (
        await BranchGatingSlotRule().check(
            cmd("set_slot", {"slot": "nome", "value": "x"}), estado, ctx()
        )
        is None
    )


# --------------------------------------------------------------------------
# interrogative_user_msg — pergunta do lead não é resposta
# --------------------------------------------------------------------------


async def test_pergunta_do_lead_vira_clarify_em_vez_de_resposta(estado) -> None:
    estado["_last_user_msg"] = "vocês tem outro modelo?"
    c = ctx(
        current_node_def={
            "id": "c_um",
            "kind": "collect",
            "field": "servico",
            "validator": "sim_nao",
            "prompt": "quer seguir?",
        }
    )
    r = await InterrogativeUserMsgRule().check(
        cmd("set_slot", {"slot": "servico", "value": "sim"}), estado, c
    )
    assert r is not None
    assert r.transform is not None and r.transform.to_kind == "clarify"


async def test_afirmacao_do_lead_passa_normalmente(estado) -> None:
    estado["_last_user_msg"] = "sim, pode seguir"
    c = ctx(
        current_node_def={
            "id": "c_um",
            "kind": "collect",
            "field": "servico",
            "validator": "sim_nao",
        }
    )
    assert (
        await InterrogativeUserMsgRule().check(
            cmd("set_slot", {"slot": "servico", "value": "sim"}), estado, c
        )
        is None
    )


# --------------------------------------------------------------------------
# slot_validator — valor tem que caber no enum declarado
# --------------------------------------------------------------------------


async def test_valor_fora_do_enum_e_dropado(estado) -> None:
    c = ctx(slot_enums={"servico": ["corte", "barba", "combo"]})
    r = await SlotValidatorRule().check(
        cmd("set_slot", {"slot": "servico", "value": "pinguim"}), estado, c
    )
    assert r is not None and r.code == "enum_value_unresolvable"


async def test_valor_do_enum_dito_com_outras_palavras_e_coagido(estado) -> None:
    """ "queria fazer a barba" → barba. Coerção, não rejeição."""
    c = ctx(slot_enums={"servico": ["corte", "barba", "combo"]})
    r = await SlotValidatorRule().check(
        cmd("set_slot", {"slot": "servico", "value": "queria fazer a barba"}), estado, c
    )
    assert r is not None and r.code == "enum_value_coerced"
    assert r.transform is not None and r.transform.payload_overrides["value"] == "barba"


async def test_valor_ja_canonico_passa_direto(estado) -> None:
    c = ctx(slot_enums={"servico": ["corte", "barba"]})
    assert (
        await SlotValidatorRule().check(
            cmd("set_slot", {"slot": "servico", "value": "corte"}), estado, c
        )
        is None
    )


async def test_enum_sim_nao_usa_o_normalizador_rico(estado) -> None:
    c = ctx(slot_enums={"topa": ["sim", "nao"]})
    r = await SlotValidatorRule().check(
        cmd("set_slot", {"slot": "topa", "value": "claro que sim"}), estado, c
    )
    assert r is not None and r.transform is not None
    assert r.transform.payload_overrides["value"] == "sim"


# --------------------------------------------------------------------------
# appointment_slot_scope — horário fabricado morre antes do booking
# --------------------------------------------------------------------------


async def test_horario_fora_da_agenda_e_rejeitado(estado) -> None:
    estado["collected"]["available_slots"] = {"slots": [{"slot_id": "2026-08-25T14:00"}]}
    r = await AppointmentSlotScopeRule().check(
        cmd("set_slot", {"slot": "slot_escolhido", "value": "2026-08-25T09:00"}), estado, ctx()
    )
    assert r is not None and r.code == "slot_not_in_offer"


async def test_horario_da_agenda_e_aceito(estado) -> None:
    estado["collected"]["available_slots"] = {"slots": [{"slot_id": "2026-08-25T14:00"}]}
    assert (
        await AppointmentSlotScopeRule().check(
            cmd("set_slot", {"slot": "slot_escolhido", "value": "2026-08-25T14:00"}), estado, ctx()
        )
        is None
    )


# --------------------------------------------------------------------------
# subflow_v4
# --------------------------------------------------------------------------


async def test_subflow_com_ref_desconhecida_e_rejeitado(estado) -> None:
    r = await SubflowRule().check(
        cmd("start_subflow", {"ref": "sub_fantasma"}), estado, ctx(subflow_registry_refs=["sub_ok"])
    )
    assert r is not None and r.code == "subflow_unknown_ref"


async def test_subflow_ja_na_pilha_e_loop(estado) -> None:
    estado["subflow_stack"] = [{"ref": "sub_ok"}]
    r = await SubflowRule().check(
        cmd("start_subflow", {"ref": "sub_ok"}), estado, ctx(subflow_registry_refs=["sub_ok"])
    )
    assert r is not None and r.code == "subflow_loop"


async def test_subflow_sem_inputs_obrigatorios_e_rejeitado(estado) -> None:
    r = await SubflowRule().check(
        cmd("start_subflow", {"ref": "sub_ok", "inputs": {}}),
        estado,
        ctx(subflow_registry_refs=["sub_ok"], subflow_required_inputs={"sub_ok": ["produto"]}),
    )
    assert r is not None and r.code == "subflow_missing_inputs"


async def test_cancelar_subflow_inexistente_e_rejeitado(estado) -> None:
    r = await SubflowRule().check(cmd("cancel_subflow", {"reason": "mudou"}), estado, ctx())
    assert r is not None and r.code == "nothing_to_cancel"


# --------------------------------------------------------------------------
# plan_reach
# --------------------------------------------------------------------------


async def test_replan_para_no_inexistente_e_rejeitado(estado) -> None:
    plano = {
        "new_plan": [
            {"step_id": "s1", "intent": "ask_group", "target": "n_fantasma", "rationale": "r"}
        ],
        "reason": "lead pulou etapa",
    }
    r = await PlanReachabilityRule().check(cmd("replan", plano), estado, ctx())
    assert r is not None and r.code == "plan_unreachable"


async def test_replan_para_no_alcancavel_passa(estado) -> None:
    plano = {
        "new_plan": [{"step_id": "s1", "intent": "decide", "target": "d_um", "rationale": "r"}],
        "reason": "ok",
    }
    assert await PlanReachabilityRule().check(cmd("replan", plano), estado, ctx()) is None


# --------------------------------------------------------------------------
# say_scope — despedida escrita por humano não é sobrescrita por LLM
# --------------------------------------------------------------------------


async def test_say_fora_de_freetalk_ou_end_e_rejeitado(estado) -> None:
    r = await SayScopeRule().check(
        cmd("say_freetalk", {"text": "oi"}), estado, ctx(current_node_def={"kind": "collect_group"})
    )
    assert r is not None and r.code == "say_outside_freetalk_or_end"


async def test_llm_nao_sobrescreve_farewell_autorado(estado) -> None:
    r = await SayScopeRule().check(
        cmd("say_freetalk", {"text": "tchau!"}),
        estado,
        ctx(current_node_def={"kind": "end", "farewell": "Fechado! Te espero."}),
    )
    assert r is not None and r.code == "say_overrides_authored_farewell"


async def test_end_sem_farewell_aceita_despedida_do_llm(estado) -> None:
    assert (
        await SayScopeRule().check(
            cmd("say_freetalk", {"text": "tchau!"}), estado, ctx(current_node_def={"kind": "end"})
        )
        is None
    )


# --------------------------------------------------------------------------
# signal_normalize / signal_guard / signal_rate
# --------------------------------------------------------------------------


async def test_nome_de_sinal_quase_certo_e_canonizado(estado) -> None:
    """Bug real: o extrator emitiu `pedi_corretor` para `pediu_corretor`."""
    r = await SignalNameNormalizeRule().check(
        cmd("signal", {"name": "pedi_corretor", "value": True}),
        estado,
        ctx(node_signals=["pediu_corretor", "desistiu"]),
    )
    assert r is not None and r.transform is not None
    assert r.transform.payload_overrides["name"] == "pediu_corretor"


async def test_nome_ambiguo_nao_e_adivinhado(estado) -> None:
    """Entre dois sinais igualmente próximos, a rule se recusa a chutar."""
    assert (
        await SignalNameNormalizeRule().check(
            cmd("signal", {"name": "ajustar_x", "value": True}),
            estado,
            ctx(node_signals=["ajustar_preco", "ajustar_tipo"]),
        )
        is None
    )


async def test_sinal_sensivel_exige_evidencia_na_fala(estado) -> None:
    """`recusou` era emitido em qualquer frase com "não"."""
    estado["_last_user_msg"] = "não sei o número certinho"
    c = ctx(node_signals=["recusou"], business={"guarded_signals": {"recusou": ["não quero"]}})
    r = await SignalGuardRule().check(cmd("signal", {"name": "recusou", "value": True}), estado, c)
    assert r is not None and r.code == "signal_guard_no_match"


async def test_sinal_sensivel_com_evidencia_passa(estado) -> None:
    estado["_last_user_msg"] = "não quero, obrigado"
    c = ctx(node_signals=["recusou"], business={"guarded_signals": {"recusou": ["não quero"]}})
    assert (
        await SignalGuardRule().check(cmd("signal", {"name": "recusou", "value": True}), estado, c)
        is None
    )


async def test_apenas_um_sinal_por_turno(estado) -> None:
    r = await SignalRateLimitRule().check(
        cmd("signal", {"name": "escolheu", "value": True}),
        estado,
        ctx(node_signals=["escolheu"], signals_emitted_this_turn=1),
    )
    assert r is not None and r.code == "signal_rate_limited"


async def test_sinal_nao_declarado_no_no_e_rejeitado(estado) -> None:
    r = await SignalRateLimitRule().check(
        cmd("signal", {"name": "inventado", "value": True}), estado, ctx(node_signals=["escolheu"])
    )
    assert r is not None and r.code == "signal_unknown_in_node"


# --------------------------------------------------------------------------
# skip_collect_rate / confidence
# --------------------------------------------------------------------------


async def test_skips_consecutivos_demais_e_drift_do_extrator(estado) -> None:
    estado["_skip_collect_count"] = 3
    r = await SkipCollectRateLimitRule().check(
        cmd("skip_collect", {"node_id": "c_um", "reason": "lead nao quis"}), estado, ctx()
    )
    assert r is not None and r.code == "excessive_skip_collect"


async def test_confianca_baixa_vira_confirmacao(estado) -> None:
    r = await ConfidenceThresholdRule().check(
        cmd("set_slot", {"slot": "nome", "value": "Ana"}, confidence=0.4), estado, ctx()
    )
    assert r is not None and r.transform is not None
    assert r.transform.to_kind == "confirm_slot"


async def test_confirmacao_ja_pendente_nao_vira_loop(estado) -> None:
    """Re-transformar um slot já em confirmação girava para sempre."""
    estado["pending_confirmations"] = {"nome": "Ana"}
    assert (
        await ConfidenceThresholdRule().check(
            cmd("set_slot", {"slot": "nome", "value": "Ana"}, confidence=0.4), estado, ctx()
        )
        is None
    )


# --------------------------------------------------------------------------
# permission / cost_cap / socializer
# --------------------------------------------------------------------------


async def test_papel_sem_permissao_nao_emite_o_comando(estado) -> None:
    r = await PermissionRule(allowed_kinds={"leitor": {"clarify"}}).check(
        cmd("set_slot", {"slot": "nome", "value": "x"}), estado, ctx(role="leitor")
    )
    assert r is not None and r.code == "command_not_allowed_for_role"


async def test_handoff_ignora_restricao_de_papel(estado) -> None:
    """Escape universal: se o lead pede humano, nenhuma rule bloqueia."""
    assert (
        await PermissionRule(allowed_kinds={"leitor": {"clarify"}}).check(
            cmd("handoff_human", {"reason": "pediu vendedor"}), estado, ctx(role="leitor")
        )
        is None
    )


def test_allowlist_padrao_do_papel_agent_cobre_os_kinds_operacionais() -> None:
    permitidos = DEFAULT_AGENT_ALLOWED_KINDS["agent"]
    assert {"set_slot", "signal", "clarify", "handoff_human", "finish_flow"} <= permitidos


async def test_teto_de_custo_por_turno(estado) -> None:
    r = await CostCapRule(max_usd_per_turn=0.05).check(
        cmd("set_slot", {"slot": "nome", "value": "x"}), estado, ctx(turn_usd=0.9)
    )
    assert r is not None and r.code == "turn_budget_exceeded"


async def test_teto_de_custo_da_conversa(estado) -> None:
    r = await CostCapRule(max_budget_usd=1.0).check(
        cmd("set_slot", {"slot": "nome", "value": "x"}), estado, ctx(root_conversation_usd=5.0)
    )
    assert r is not None and r.code == "root_budget_exceeded"


async def test_socializer_scope_e_no_op_por_enquanto(estado) -> None:
    """Mantida por paridade: nenhum call site liga `in_socializer` ainda."""
    assert (
        await SocializerScopeRule().check(
            cmd("set_slot", {"slot": "nome", "value": "x"}), estado, ctx()
        )
        is None
    )


# --------------------------------------------------------------------------
# collect_group_exit / finish_flow_graph / album_scope
# --------------------------------------------------------------------------


async def test_grupo_de_coleta_estoura_max_turns(estado) -> None:
    estado["turns_in_node"] = 5
    c = ctx(current_node_def={"id": "c_um", "kind": "collect_group", "max_turns": 3})
    r = await CollectGroupExitRule().check(cmd("clarify", {"question": "?"}), estado, c)
    assert r is not None and r.code == "collect_group_max_turns_exceeded"


async def test_finish_flow_so_vale_num_no_end(estado) -> None:
    """O extrator tendia a encerrar a conversa logo após a última coleta."""
    r = await FinishFlowGraphRule().check(
        cmd("finish_flow", {"outcome": "completed"}),
        estado,
        ctx(current_node_def={"kind": "collect_group"}),
    )
    assert r is not None and r.code == "finish_flow_premature"
    assert r.force_decision == "soft", "precisa vencer a política 'never' do kind"


async def test_finish_flow_num_end_passa(estado) -> None:
    assert (
        await FinishFlowGraphRule().check(
            cmd("finish_flow", {"outcome": "completed"}),
            estado,
            ctx(current_node_def={"kind": "end"}),
        )
        is None
    )


async def test_album_de_item_inexistente_e_rejeitado(estado) -> None:
    estado["collected"]["search_result"] = {"candidates": [{"codigo": "AP-001", "fotos": ["u"]}]}
    r = await AlbumScopeRule().check(cmd("send_album", {"item_id": "AP-999"}), estado, ctx())
    assert r is not None and r.code == "item_not_in_candidates"


async def test_album_de_item_apresentado_passa(estado) -> None:
    estado["collected"]["search_result"] = {"candidates": [{"codigo": "AP-001", "fotos": ["u"]}]}
    assert (
        await AlbumScopeRule().check(cmd("send_album", {"item_id": "AP-001"}), estado, ctx())
        is None
    )


async def test_album_de_item_sem_foto_e_rejeitado(estado) -> None:
    """Prometer foto que não existe é pior que dizer que não tem."""
    estado["collected"]["search_result"] = {"candidates": [{"codigo": "AP-001"}]}
    r = await AlbumScopeRule().check(cmd("send_album", {"item_id": "AP-001"}), estado, ctx())
    assert r is not None and r.code == "item_no_photo"


# --------------------------------------------------------------------------
# pii — construção sem Presidio instalado não pode explodir
# --------------------------------------------------------------------------


async def test_pii_rule_e_fail_soft(estado) -> None:
    """Presidio quebrado degrada para regex; nunca derruba o turno."""
    from zoi_agno.enforcement.pii_v4 import PIIRule

    class PipelineQuebrado:
        def redact(self, text: str):
            raise RuntimeError("spaCy ausente")

    r = await PIIRule(pipeline=PipelineQuebrado()).check(
        cmd("say_freetalk", {"text": "meu cpf é 123.456.789-00"}), estado, ctx()
    )
    assert r is None, "erro no pipeline não pode bloquear o turno"


@pytest.mark.parametrize("kind", ["set_slot", "signal", "clarify"])
async def test_pii_so_olha_say_freetalk(kind: str, estado) -> None:
    from zoi_agno.enforcement.pii_v4 import PIIRule

    payloads = {
        "set_slot": {"slot": "nome", "value": "x"},
        "signal": {"name": "s", "value": 1},
        "clarify": {"question": "?"},
    }

    class NuncaChamado:
        def redact(self, text: str):
            raise AssertionError("não deveria ser chamado")

    assert (
        await PIIRule(pipeline=NuncaChamado()).check(cmd(kind, payloads[kind]), estado, ctx())
        is None
    )
