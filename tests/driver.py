"""Motorista determinístico — percorre uma routine sem LLM nenhum.

Substitui os quatro cérebros por escolhas fixas: preenche slot obrigatório com
o primeiro valor válido, escolhe o k-ésimo sinal em cada ``freetalk``, devolve
payload sintético de cada ``tool``.

Serve a duas coisas:

1. **Aceite da Fase 3** — o executor leva qualquer routine do início a um
   ``end``, sem travar e sem girar.
2. **Cobertura de desfecho** — variando ``k``, percorre caminhos diferentes e
   revela quais ``end`` são alcançáveis de fato, não só no papel.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from zoi_routine.ast import (
    CollectGroupNode,
    CollectNode,
    DecideNode,
    FieldBranch,
    FreeTalkNode,
    RoutineAst,
    SignalBranch,
    ToolNode,
)

from zoi_agno.executor import advance, current_node, missing_required
from zoi_agno.state import new_session_state


class NaoTerminou(RuntimeError):
    """A travessia estourou o limite de turnos sem chegar a um ``end``."""


@dataclass
class Travessia:
    """O que aconteceu ao percorrer a routine."""

    end_id: str = ""
    turnos: int = 0
    visitados: list[str] = field(default_factory=list)
    slots: dict[str, Any] = field(default_factory=dict)
    despedidas: list[str] = field(default_factory=list)


def _valor_para(routine: RoutineAst, slot: str, k: int) -> Any:
    """Um valor válido para o slot — enum escolhe o k-ésimo declarado."""
    decl = routine.slots.get(slot)
    if decl is None:
        return "x"
    if decl.type == "enum" and decl.values:
        return decl.values[k % len(decl.values)]
    if decl.type == "number":
        return 1
    if decl.type == "boolean":
        return True
    if decl.type == "datetime":
        return "2026-08-25T14:00"
    return f"valor_{slot}"


def percorrer(
    routine: RoutineAst,
    *,
    k: int = 0,
    max_turnos: int = 60,
    tenant_id: str = "t_teste",
) -> Travessia:
    """Percorre a routine do início até um ``end``.

    ``k`` seleciona a estratégia: qual valor de enum e qual sinal escolher
    quando há mais de uma opção. Rodar com vários ``k`` cobre ramos distintos.
    """
    st = new_session_state(
        thread_id="drv", tenant_id=tenant_id, contact_id="1", start_node=routine.start_node_id
    )
    t = Travessia()

    for turno in range(max_turnos):
        r = advance(routine, st)
        t.visitados.append(r.node_id)
        t.despedidas.extend(r.say_templates)

        if r.finished:
            t.end_id = r.node_id
            t.turnos = turno + 1
            t.slots = dict(st["collected"])
            return t

        node = current_node(routine, st)

        # tool: o executor marca pendente; aqui devolvemos um payload sintético
        if r.pending_tool is not None and isinstance(node, ToolNode):
            if node.output_to:
                st["collected"][node.output_to] = {"total": 1, "candidates": [{"codigo": "X-1"}]}
            st["current_node"] = node.next
            st["turns_in_node"] = 0
            continue

        # coleta: o lead responde
        if isinstance(node, CollectGroupNode):
            faltando = missing_required(node, st["collected"], routine)
            alvo = faltando or [f.name for f in node.fields]
            for nome in alvo:
                st["collected"][nome] = _valor_para(routine, nome, k)
            st["turns_in_node"] += 1
            continue

        if isinstance(node, CollectNode):
            st["collected"][node.slot] = _valor_para(routine, node.slot, k)
            st["turns_in_node"] += 1
            continue

        # freetalk: o lead dá um sinal
        if isinstance(node, FreeTalkNode):
            contagem = st.setdefault("_freetalk_turn_count", {})
            contagem[r.node_id] = contagem.get(r.node_id, 0) + 1
            if node.signals:
                st["last_signal"] = node.signals[k % len(node.signals)]
            for nome in node.slots:
                st["collected"].setdefault(nome, _valor_para(routine, nome, k))
            st["turns_in_node"] += 1
            continue

        # Parou num decide: algum branch depende de slot que ninguém preencheu.
        # Num turno real quem preenche é o lead, ou a derivação de slots do
        # pipeline (``prio_num`` a partir de ``prioridade_0a10``, por exemplo).
        # Aqui satisfazemos o k-ésimo branch, que é o que faz este motorista
        # ser um explorador de alcançabilidade e não só um caminho feliz.
        if isinstance(node, DecideNode):
            if _satisfazer_branch(node, st, routine, k):
                continue
            raise NaoTerminou(f"decide {r.node_id!r} sem branch satisfazível — motivo: {r.reason}")

        # Nada a fazer e não terminou: o executor não sabe sair daqui.
        raise NaoTerminou(f"travado em {r.node_id!r} ({type(node).__name__}) — motivo: {r.reason}")

    raise NaoTerminou(f"{max_turnos} turnos sem alcançar um end; últimos: {t.visitados[-6:]}")


def _satisfazer_branch(node: DecideNode, st: dict[str, Any], routine: RoutineAst, k: int) -> bool:
    """Preenche o que o k-ésimo branch do decide exige. ``False`` se impossível.

    Percorre os branches a partir do k-ésimo (com wrap) e para no primeiro que
    consegue satisfazer: campo com ``on_value`` recebe esse valor; campo por
    presença recebe um valor válido; sinal declarado é emitido.
    """
    branches = list(node.branches)
    if not branches:
        return False
    for i in range(len(branches)):
        b = branches[(k + i) % len(branches)]
        if isinstance(b, FieldBranch):
            raiz = b.on_field.split(".")[0]
            if b.on_value is not None:
                st["collected"][raiz] = b.on_value
            else:
                st["collected"][raiz] = _valor_para(routine, raiz, k)
            return True
        if isinstance(b, SignalBranch):
            st["last_signal"] = b.on_signal
            return True
    return False
