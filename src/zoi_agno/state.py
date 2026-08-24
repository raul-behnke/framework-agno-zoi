"""O estado de uma conversa — o ``session_state`` do Workflow.

No runtime v4 isso era o ``AgentLoopState`` do LangGraph, com reducers por
canal. No Agno o estado é um dicionário simples: um step-função que declare
``run_context`` recebe o dicionário **vivo**, e o Agno o persiste no ``db``
por ``session_id`` ao fim do run. Sem reducers, sem canais.

Convenção herdada do v4 e mantida de propósito: chaves com prefixo ``_`` são
**efêmeras de turno ou internas do runtime** — nunca fazem parte do contrato
com o autor da routine, e o autor não pode referenciá-las no YAML.

O que sobreviveu do v4 é o essencial de uma conversa em curso; ficou de fora
tudo que era acoplado a LangGraph (reducers ``operator.add``), a canal GHL ou
a subsistemas fora do gate da Fase 4.
"""

from __future__ import annotations

from typing import Any, TypedDict


class SessionState(TypedDict, total=False):
    """Forma do ``session_state``. ``total=False``: toda chave é opcional.

    Serve como documentação executável e como alvo de type-check — em runtime
    o Agno trafega um ``dict`` comum.
    """

    # --- identidade da conversa ---
    thread_id: str
    tenant_id: str
    contact_id: str
    routine_version: str

    # --- posição no grafo ---
    current_node: str
    turns_in_node: int
    scope: str  # "main" ou o nome da sub-rotina ativa
    subflow_stack: list[dict[str, Any]]

    # --- o que o lead já disse ---
    collected: dict[str, Any]
    pending_confirmations: dict[str, Any]
    last_signal: str | None

    # --- saída ---
    outgoing_message: str | None
    say_renders: list[str]

    # --- rastro (auditoria e depuração) ---
    messages_tail: list[dict[str, Any]]
    command_log: list[dict[str, Any]]
    enforcement_rejections: list[dict[str, Any]]
    tool_call_log: list[dict[str, Any]]
    skipped_nodes: list[str]
    state_summary: str

    # --- plano (o Planner é um dos 5 cérebros) ---
    plan: dict[str, Any] | None
    plan_history: list[dict[str, Any]]

    # --- contadores de anti-loop; alimentam as rules de rate limit ---
    _turn_counter: int
    _turns_since_set_slot: int
    _skip_collect_count: int
    _drift_streak: int
    _freetalk_turn_count: dict[str, int]
    _recommend_rounds: dict[str, int]
    _objection_counts: dict[str, int]

    # --- efêmeros do turno ---
    _last_user_msg: str
    _last_accepted_commands: list[dict[str, Any]]
    _turn_usd: float
    _turn_ms: int

    # --- foco de catálogo (qual item o lead está olhando) ---
    _picked_candidate: dict[str, Any] | None
    _presented_candidates: list[dict[str, Any]]

    # --- wait ---
    _waiting: bool


def new_session_state(
    *,
    thread_id: str,
    tenant_id: str,
    contact_id: str,
    start_node: str,
    routine_version: str = "",
) -> dict[str, Any]:
    """Estado inicial de uma conversa nova.

    Todo container é criado vazio em vez de ficar ausente: os steps podem
    assumir que ``collected`` e ``command_log`` existem, sem ``.get(k) or {}``
    espalhado por toda parte.
    """
    return {
        "thread_id": thread_id,
        "tenant_id": tenant_id,
        "contact_id": contact_id,
        "routine_version": routine_version,
        "current_node": start_node,
        "turns_in_node": 0,
        "scope": "main",
        "subflow_stack": [],
        "collected": {},
        "pending_confirmations": {},
        "last_signal": None,
        "outgoing_message": None,
        "say_renders": [],
        "messages_tail": [],
        "command_log": [],
        "enforcement_rejections": [],
        "tool_call_log": [],
        "skipped_nodes": [],
        "state_summary": "",
        "plan": None,
        "plan_history": [],
        "_turn_counter": 0,
        "_turns_since_set_slot": 0,
        "_skip_collect_count": 0,
        "_drift_streak": 0,
        "_freetalk_turn_count": {},
        "_recommend_rounds": {},
        "_objection_counts": {},
        "_last_user_msg": "",
        "_last_accepted_commands": [],
        "_picked_candidate": None,
        "_presented_candidates": [],
        "_waiting": False,
    }


def reset_turn(state: dict[str, Any]) -> None:
    """Zera o que só vale dentro de um turno.

    Chamado no início do pipeline. Sem isso, uma rejeição de enforcement de
    três turnos atrás ainda apareceria no prompt do redator — bug real do v4
    (``enforcement_rejections`` acumulava até o fim da conversa).
    """
    state["enforcement_rejections"] = []
    state["_last_accepted_commands"] = []
    state["say_renders"] = []
    state["outgoing_message"] = None
    state["_turn_counter"] = int(state.get("_turn_counter", 0)) + 1
