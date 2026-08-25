"""Os 15 comandos — o vocabulário fechado de mutação de estado."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from zoi_agno.contracts import Command, CommandGenOutput, CommandKind

KINDS_ESPERADOS = {
    "set_slot",
    "confirm_slot",
    "skip_collect",
    "start_subflow",
    "cancel_subflow",
    "clarify",
    "replan",
    "handoff_human",
    "say_freetalk",
    "signal",
    "finish_flow",
    "record_fact",
    "annotate_interaction",
    "send_album",
}


def test_sao_exatamente_quatorze_kinds() -> None:
    """Guarda de contagem: um kind novo exige tocar 5 construtos em lockstep.

    Portado do v4 de propósito — os goldens comparam os dois runtimes, então
    o vocabulário tem que ser idêntico dos dois lados.

    DIVERGÊNCIA DELIBERADA do v4: ``consult_faq`` não existe aqui. O v4 tem
    RAG para atender esse comando; este runtime não populou ``_faq_chunks``
    em lugar nenhum, então o kind era aceito pelas rules e descartado em
    silêncio por ``pipeline._aplicar`` — um comando que o extrator adorava
    emitir e que queimava o turno inteiro sem mudar estado. Removido do
    vocabulário: o que o modelo não pode emitir, ele não emite.
    """
    from typing import get_args

    assert set(get_args(CommandKind)) == KINDS_ESPERADOS
    assert len(KINDS_ESPERADOS) == 14


def test_uniao_discriminada_escolhe_o_payload_certo() -> None:
    cmd = Command.model_validate(
        {"kind": "set_slot", "payload": {"slot": "nome", "value": "Mariana"}}
    )
    assert cmd.kind == "set_slot"
    assert cmd.payload.slot == "nome"
    assert cmd.payload.value == "Mariana"
    assert cmd.confidence == 1.0


def test_payload_errado_para_o_kind_e_rejeitado() -> None:
    """O discriminador impede um clarify vestido de set_slot."""
    with pytest.raises(ValidationError):
        Command.model_validate({"kind": "set_slot", "payload": {"question": "qual seu nome?"}})


def test_kind_desconhecido_e_rejeitado() -> None:
    with pytest.raises(ValidationError):
        Command.model_validate({"kind": "apagar_tudo", "payload": {}})


def test_confidence_fora_do_intervalo_e_rejeitada() -> None:
    with pytest.raises(ValidationError):
        Command.model_validate(
            {"kind": "signal", "payload": {"name": "x", "value": 1}, "confidence": 1.5}
        )


def test_rationale_longa_e_truncada_em_vez_de_falhar() -> None:
    """Modelo verboso não pode derrubar o turno inteiro.

    Precedente real no v4: DeepSeek em json_object ignora ``maxLength`` do
    schema e escrevia rationale gigante → ValidationError → o turno caía no
    fallback. O rationale é interno (auditoria), então cortar é barato;
    perder o comando não é.
    """
    cmd = Command.model_validate(
        {
            "kind": "handoff_human",
            "payload": {"reason": "quer falar com vendedor"},
            "rationale": "x" * 5000,
        }
    )
    assert len(cmd.rationale) == 200


def test_finish_flow_so_aceita_desfechos_conhecidos() -> None:
    Command.model_validate({"kind": "finish_flow", "payload": {"outcome": "handed_off"}})
    with pytest.raises(ValidationError):
        Command.model_validate({"kind": "finish_flow", "payload": {"outcome": "vendido"}})


def test_lote_do_extrator_tem_teto_de_doze_comandos() -> None:
    """Multi-extract é desejável; 50 comandos num turno é alucinação."""
    ok = CommandGenOutput.model_validate(
        {
            "commands": [
                {"kind": "set_slot", "payload": {"slot": f"s{i}", "value": i}} for i in range(12)
            ]
        }
    )
    assert len(ok.commands) == 12
    with pytest.raises(ValidationError):
        CommandGenOutput.model_validate(
            {
                "commands": [
                    {"kind": "set_slot", "payload": {"slot": f"s{i}", "value": i}}
                    for i in range(13)
                ]
            }
        )


def test_lote_vazio_e_valido() -> None:
    """Lead que só diz 'oi' não gera comando nenhum — e isso é normal."""
    assert CommandGenOutput.model_validate({"commands": []}).commands == []


def test_model_dump_compat_para_auditoria() -> None:
    cmd = Command.model_validate({"kind": "clarify", "payload": {"question": "qual cidade?"}})
    d = cmd.model_dump_compat()
    assert d["kind"] == "clarify"
    assert d["payload"]["question"] == "qual cidade?"
    assert "confidence" in d and "rationale" in d


@pytest.mark.parametrize("kind", sorted(KINDS_ESPERADOS))
def test_todo_kind_tem_um_exemplo_valido(kind: str) -> None:
    """Cada kind precisa ser construível — pega payload class não fiada."""
    exemplos: dict[str, dict] = {
        "set_slot": {"slot": "nome", "value": "x"},
        "confirm_slot": {"slot": "nome", "proposed_value": "x"},
        "skip_collect": {"node_id": "c_um", "reason": "lead recusou"},
        "start_subflow": {"ref": "sub_qualificacao"},
        "cancel_subflow": {"reason": "lead mudou de assunto"},
        "clarify": {"question": "pode repetir?"},
        "replan": {
            "new_plan": [
                {"step_id": "s1", "intent": "ask_group", "target": "c_um", "rationale": "r"}
            ],
            "reason": "lead pulou etapa",
        },
        "handoff_human": {"reason": "pediu vendedor"},
        "say_freetalk": {"text": "claro, posso ajudar"},
        "signal": {"name": "escolheu", "value": True},
        "finish_flow": {"outcome": "completed"},
        "record_fact": {"name": "tem_loja", "value": True},
        "annotate_interaction": {"subtype": "objecao_preco"},
        "send_album": {"item_id": "AP-001"},
    }
    cmd = Command.model_validate({"kind": kind, "payload": exemplos[kind]})
    assert cmd.kind == kind
