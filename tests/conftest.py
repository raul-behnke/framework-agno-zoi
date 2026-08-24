from __future__ import annotations

from typing import Any

import pytest

from zoi_agno.contracts import Command
from zoi_agno.state import new_session_state


@pytest.fixture
def estado() -> dict[str, Any]:
    """Estado de conversa novo, no nó ``c_um``."""
    return new_session_state(
        thread_id="tg:1", tenant_id="t_demo", contact_id="1", start_node="c_um"
    )


def ctx(**over: Any) -> dict[str, Any]:
    """Contexto de runtime que as rules leem.

    Mantido explícito nos testes em vez de derivado do RuntimeContext real —
    uma rule deve ser testável sozinha, sem montar um Workflow.
    """
    base: dict[str, Any] = {
        "flow_slots": ["nome", "cidade", "servico"],
        "slot_enums": {},
        "business": {},
        "current_node_def": {"id": "c_um", "kind": "collect_group"},
        "node_signals": [],
        "reachable_nodes": ["c_um", "d_um", "e_fim"],
        "subflow_registry_refs": [],
        "subflow_required_inputs": {},
        "role": None,
        "turn_usd": 0.0,
        "root_conversation_usd": 0.0,
        "signals_emitted_this_turn": 0,
        "is_advance_intent": False,
    }
    base.update(over)
    return base


def cmd(kind: str, payload: dict[str, Any], **over: Any) -> Command:
    """Atalho para montar um Command válido."""
    return Command.model_validate({"kind": kind, "payload": payload, **over})


FIXTURES_TENANTS = __import__("pathlib").Path(__file__).parent / "fixtures" / "tenants"
