"""A ordem da fila de rules é semântica. Este teste a congela.

Duas inversões que já causaram bug de produção no v4:

1. ``interrogative_user_msg`` **antes** de ``slot_validator`` — a pergunta do
   lead ("tem outro modelo?") não pode virar um ``sim`` alucinado que passa
   pela checagem de forma.
2. ``signal_normalize`` **antes** de ``signal_rate`` — o normalizador canoniza
   ``pedi_corretor`` → ``pediu_corretor``; invertido, o portão de match exato
   rejeita, o turno entra em loop e o subflow destrancado pelo sinal nunca é
   alcançado.

Se você precisa mudar a ordem, mude aqui também — de propósito.
"""

from __future__ import annotations

from zoi_agno.enforcement import RULE_ORDER, default_rules


def test_a_fila_construida_segue_a_ordem_declarada() -> None:
    assert tuple(r.name for r in default_rules()) == RULE_ORDER


def test_sao_vinte_rules() -> None:
    assert len(RULE_ORDER) == 20
    assert len(set(RULE_ORDER)) == 20, "nome de rule duplicado"


def test_toda_rule_expoe_name_e_check() -> None:
    for r in default_rules():
        assert isinstance(r.name, str) and r.name
        assert callable(r.check)


def _pos(nome: str) -> int:
    return RULE_ORDER.index(nome)


def test_pergunta_do_lead_e_avaliada_antes_da_validacao_de_valor() -> None:
    assert _pos("interrogative_user_msg") < _pos("slot_validator")


def test_normalizacao_de_sinal_vem_antes_do_portao_de_match_exato() -> None:
    assert _pos("signal_normalize") < _pos("signal_rate")


def test_guarda_de_sinal_roda_com_o_nome_ja_canonico() -> None:
    assert _pos("signal_normalize") < _pos("signal_guard")


def test_escopo_de_slot_vem_antes_de_qualquer_coercao() -> None:
    """Slot fora de escopo morre antes de ser coagido para um valor bonito."""
    assert _pos("slot_scope") < _pos("slot_validator")
    assert _pos("slot_scope") < _pos("confidence")


def test_validacao_semantica_vem_antes_da_transformacao_por_confianca() -> None:
    """Valor inválido vira clarify na hora, sem passar por confirm_slot."""
    assert _pos("slot_validator") < _pos("confidence")
