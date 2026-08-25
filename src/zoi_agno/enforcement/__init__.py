"""A fiscalização — entre a intenção do LLM e o efeito no mundo.

O extrator emite comandos; o dispatcher passa cada um por esta fila de rules,
**nesta ordem**, e cada rule pode:

- deixar passar (devolve ``None``)
- **soft**: descartar o comando e seguir o lote
- **hard**: abortar o resto do lote
- **never**: reclamar mas aceitar — escape universal, só ``handoff_human`` e
  ``finish_flow``. Se o lead pede um humano, nada bloqueia
- **transform**: reescrever o comando (ex.: confiança baixa → ``confirm_slot``)

A ordem é semântica, não estilo. Dois exemplos que custaram bug de produção:

``InterrogativeUserMsgRule`` roda **antes** de ``SlotValidatorRule`` — senão a
pergunta do lead ("tem outro?") vira um ``sim`` alucinado que passa pela
checagem de forma.

``SignalNameNormalizeRule`` roda **antes** de ``SignalRateLimitRule`` — o
normalizador canoniza ``pedi_corretor`` → ``pediu_corretor``, e só então o
portão de match exato avalia. Invertido, o turno entra em loop e nunca alcança
o subflow que o sinal destranca.

``tests/test_enforcement_order.py`` trava a sequência.
"""

from __future__ import annotations

from typing import Any

from zoi_agno.enforcement.rule import EnforcementRuleV4, Rejection, Transform

# A ordem canônica, espelhando ``turn_manager.py::_default_rules`` do v4.
# Mudar a ordem exige mudar o teste que a trava — de propósito.
RULE_ORDER: tuple[str, ...] = (
    "slot_scope",
    "branch_gating_slot",
    "interrogative_user_msg",
    "slot_validator",
    "appointment_slot_scope",
    "catalog_choice_scope",
    "subflow_v4",
    "plan_reach",
    "say_scope",
    "signal_normalize",
    "signal_guard",
    "signal_rate",
    "skip_collect_rate",
    "confidence",
    "pii_v4",
    "permission_v4",
    "cost_cap_v4",
    "socializer_scope_v4",
    "collect_group_exit",
    "finish_flow_graph",
    "album_scope",
)


def default_rules(*, metrics: Any = None, presidio: Any = None) -> list[EnforcementRuleV4]:
    """Constrói a fila canônica de rules, na ordem de ``RULE_ORDER``.

    Imports tardios mantêm a inicialização do Presidio fora do caminho quente
    até a primeira chamada.
    """
    from zoi_agno.enforcement.album_scope import AlbumScopeRule
    from zoi_agno.enforcement.appointment_slot_scope import AppointmentSlotScopeRule
    from zoi_agno.enforcement.branch_gating_slot import BranchGatingSlotRule
    from zoi_agno.enforcement.catalog_choice_scope import CatalogChoiceScopeRule
    from zoi_agno.enforcement.collect_group_exit import CollectGroupExitRule
    from zoi_agno.enforcement.confidence import ConfidenceThresholdRule
    from zoi_agno.enforcement.cost_cap_v4 import CostCapRule
    from zoi_agno.enforcement.finish_flow_graph import FinishFlowGraphRule
    from zoi_agno.enforcement.interrogative_user import InterrogativeUserMsgRule
    from zoi_agno.enforcement.permission_v4 import PermissionRule
    from zoi_agno.enforcement.pii_v4 import PIIRule
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

    return [
        # Contenção anti-fabricação: o slot tem que pertencer ao escopo ativo.
        SlotScopeRule(),
        # Slot que dirige um Decide só pode ser setado no nó que o coleta.
        BranchGatingSlotRule(),
        # Pergunta do lead nunca vira resposta extraída. ANTES do validator.
        InterrogativeUserMsgRule(),
        # Valor tem que resolver para o enum declarado; coage quando dá.
        SlotValidatorRule(),
        # Horário escolhido ∈ agenda oferecida — mata booking fabricado.
        AppointmentSlotScopeRule(),
        CatalogChoiceScopeRule(),
        SubflowRule(),
        PlanReachabilityRule(),
        SayScopeRule(),
        # Canoniza o nome do sinal ANTES do portão de match exato.
        SignalNameNormalizeRule(),
        # Sinal sensível exige evidência na fala do lead.
        SignalGuardRule(),
        SignalRateLimitRule(),
        # 3 skips seguidos = drift do extrator, não lead esquivo.
        SkipCollectRateLimitRule(threshold=3),
        # Confiança baixa vira confirmação em vez de virar verdade.
        ConfidenceThresholdRule(),
        PIIRule(metrics=metrics, pipeline=presidio),
        PermissionRule(),
        CostCapRule(),
        SocializerScopeRule(metrics=metrics),
        CollectGroupExitRule(),
        # finish_flow só num nó end — o extrator tende a encerrar cedo.
        FinishFlowGraphRule(),
        # item_id do álbum ∈ candidatos apresentados.
        AlbumScopeRule(),
    ]


__all__ = ["RULE_ORDER", "EnforcementRuleV4", "Rejection", "Transform", "default_rules"]
