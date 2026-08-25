"""O executor — quem move o cursor do grafo.

Este módulo é a fronteira mais importante do runtime: **nenhuma linha aqui
chama um LLM**. Ele lê o ``session_state``, olha o nó atual da routine e
decide para onde ir. O LLM já disse o que entendeu (via comandos, já
fiscalizados); a partir daqui é aritmética de grafo.

É o ``selector`` do ``Router`` do Agno — por isso a assinatura recebe e muta o
estado in-place, e devolve apenas o efeito colateral que o pipeline precisa
saber (que nó pedir a seguir, se há template a renderizar, se acabou).

Cada tipo de nó tem um resolvedor:

``collect_group``  falta slot obrigatório? fica. Estourou ``max_turns``?
                   vai para ``on_max_turns``. Senão, ``next``.
``collect``        idem, com um slot só.
``decide``         sinal ganha de campo, campo ganha de ``else``.
``tool``           marca a chamada pendente; quem executa é o pipeline.
``freetalk``       fica enquanto houver turno e nenhum sinal; senão avança.
``say``            marca o template para render e avança.
``call_subroutine`` empilha um frame e entra na sub-rotina.
``end``            sela o fluxo.
``wait``           ainda não implementado (Fase 8) — falha alto, de propósito.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from zoi_routine.ast import (
    CallSubroutineNode,
    CollectGroupNode,
    CollectNode,
    DecideNode,
    EndNode,
    FieldBranch,
    FreeTalkNode,
    Node,
    RoutineAst,
    SayNode,
    SignalBranch,
    ToolNode,
    WaitNode,
    WhenBranch,
)

from zoi_agno.executor.values import is_filled, normalize_value, resolve_path

logger = logging.getLogger(__name__)


class WaitSemRepo(RuntimeError):
    """A conversa parou num ``wait`` e não há onde registrar a espera.

    Falha alto em vez de degradar: um ``wait`` ignorado faria a conversa
    seguir como se o tempo não existisse, o que é pior que parar.
    """


@dataclass
class AdvanceResult:
    """O que mudou neste passo do executor."""

    node_id: str
    """Onde o cursor parou."""

    moved: bool = False
    """O cursor saiu do lugar?"""

    finished: bool = False
    """Pousou num ``end``."""

    pending_tool: ToolNode | None = None
    """Nó de tool a executar — o pipeline chama, o executor não."""

    say_templates: list[str] = field(default_factory=list)
    """Templates a renderizar (de um ``say`` ou do ``farewell`` de um ``end``)."""

    entered_subflow: str | None = None
    """Ref da sub-rotina em que acabou de entrar."""

    waiting: WaitNode | None = None
    """Nó ``wait`` em que a conversa estacionou. O pipeline registra a espera."""

    reason: str = ""
    """Por que parou aqui — vai para o log e para o depurador."""


# --------------------------------------------------------------------------
# escopo
# --------------------------------------------------------------------------


def current_block(routine: RoutineAst, state: dict[str, Any]):
    """O bloco ativo: ``main`` ou a sub-rotina no topo da pilha."""
    scope = state.get("scope") or "main"
    if scope == "main":
        return routine.main
    block = routine.sub_routines.get(scope)
    if block is None:
        logger.warning("executor.escopo_desconhecido scope=%r — caindo para main", scope)
        return routine.main
    return block


def current_node(routine: RoutineAst, state: dict[str, Any]) -> Node | None:
    """O nó sob o cursor, dentro do escopo ativo."""
    return current_block(routine, state).nodes.get(state.get("current_node") or "")


# --------------------------------------------------------------------------
# decide
# --------------------------------------------------------------------------


def route_decide(node: DecideNode, state: dict[str, Any]) -> str | None:
    """Escolhe o destino de um ``decide``.

    Prioridade, verificada empiricamente no v4:

    1. ``on_signal`` — casou, ganha na hora
    2. ``on_field``  — primeiro campo que casa
    3. ``else``

    ``on_value`` omitido = branch por **presença**, e presença exige valor
    não-vazio (ver ``values.is_filled``).
    """
    collected = state.get("collected") or {}
    sinal = state.get("last_signal")

    campo_casado: str | None = None
    for branch in node.branches:
        if isinstance(branch, SignalBranch):
            if sinal and branch.on_signal == sinal:
                return branch.next  # sinal ganha imediatamente
            continue
        if isinstance(branch, FieldBranch):
            if campo_casado is not None:
                continue
            valor = resolve_path(collected, branch.on_field)
            if branch.on_value is None:
                if is_filled(valor):
                    campo_casado = branch.next
            elif is_filled(valor) and normalize_value(valor) == normalize_value(branch.on_value):
                campo_casado = branch.next
            continue
        if isinstance(branch, WhenBranch):
            # JsonLogic entra junto com o motor de expressão (Fase 4). Até lá,
            # ignorar é melhor que avaliar errado.
            logger.debug("executor.when_branch_ignorado target=%s", branch.next)

    return campo_casado or node.else_


# --------------------------------------------------------------------------
# collect / collect_group
# --------------------------------------------------------------------------


def missing_required(
    node: CollectGroupNode, collected: dict[str, Any], routine: RoutineAst
) -> list[str]:
    """Slots obrigatórios do grupo que ainda não foram preenchidos.

    ``required`` no campo sobrepõe; ausente, herda da declaração do slot.
    """
    faltando: list[str] = []
    for f in node.fields:
        obrigatorio = f.required
        if obrigatorio is None:
            decl = routine.slots.get(f.name)
            obrigatorio = bool(decl.required) if decl else False
        if obrigatorio and not is_filled(collected.get(f.name)):
            faltando.append(f.name)
    return faltando


def collect_group_satisfied(
    node: CollectGroupNode, collected: dict[str, Any], routine: RoutineAst
) -> bool:
    """A política de saída do grupo foi cumprida?"""
    if node.exit_policy == "all":
        return not missing_required(node, collected, routine)
    if node.exit_policy == "any":
        return any(is_filled(collected.get(f.name)) for f in node.fields)
    if node.exit_policy == "n_of_m":
        alvo = node.exit_n or 1
        return sum(1 for f in node.fields if is_filled(collected.get(f.name))) >= alvo
    return True


# --------------------------------------------------------------------------
# o passo
# --------------------------------------------------------------------------


def advance(routine: RoutineAst, state: dict[str, Any], *, max_hops: int = 32) -> AdvanceResult:
    """Move o cursor até um nó que exige o lead, ou até o fim.

    Nós que não conversam (``decide``, ``say``, ``call_subroutine``, e um
    ``collect`` já satisfeito) são atravessados no mesmo passo — o lead não
    deve esperar um turno por uma decisão que o código já sabe tomar.

    ``max_hops`` é rede contra roteiro cíclico mal escrito: o validador do
    ``zoi-routine`` pega a maioria, mas um ciclo que só fecha em runtime (por
    valor de slot) chegaria aqui e giraria para sempre.
    """
    collected = state.get("collected") or {}
    resultado = AdvanceResult(node_id=state.get("current_node") or "")
    templates: list[str] = []
    # Nós já pisados NESTE passo. Revisitar um deles dentro da mesma chamada é
    # sempre ciclo — o estado não mudou entre os dois momentos, então o mesmo
    # roteamento se repetiria para sempre.
    #
    # Caso real (zoi_sdr): ``ft_nota`` estoura max_turns → ``exit_on_timeout``
    # aponta para ``d_prio`` → o guard de ``d_prio`` exige um slot derivado que
    # ainda não existe → ``else`` volta para ``ft_nota`` → estoura de novo.
    # Sem esta trava, o turno gira até o teto de saltos em vez de devolver a
    # palavra ao lead.
    visitados: set[str] = set()

    for _hop in range(max_hops):
        # O id do nó é a CHAVE no bloco, não um campo do nó — diferente do
        # FlowGraph do v4, onde o id viajava dentro do objeto.
        node_id = state.get("current_node") or ""
        node = current_node(routine, state)
        if node is None:
            resultado.reason = f"nó {node_id!r} não existe no escopo"
            logger.error("executor.no_inexistente %s", resultado.reason)
            break
        visitados.add(node_id)

        # --- end: sela e para ---
        if isinstance(node, EndNode):
            resultado.finished = True
            resultado.reason = "chegou a um end"
            if node.farewell:
                templates.append(node.farewell)
            break

        # --- wait: estaciona a conversa ---
        if isinstance(node, WaitNode):
            # Chegar aqui significa entrada NOVA no nó: quando o worker acorda
            # a conversa, ele já moveu o cursor para o destino antes de
            # reinvocar, então o cursor nunca volta a apontar para o wait.
            resultado.waiting = node
            state["_waiting"] = True
            resultado.reason = f"estacionado em {node_id!r} (modo {node.mode})"
            break

        # --- collect / collect_group: fica se ainda falta ---
        if isinstance(node, CollectGroupNode):
            turnos = int(state.get("turns_in_node", 0))
            if collect_group_satisfied(node, collected, routine):
                if not _move(state, node.next, resultado, visitados):
                    break
                continue
            if turnos >= node.max_turns:
                destino = node.on_max_turns or node.next
                resultado.reason = f"max_turns={node.max_turns} estourado em {node_id!r}"
                logger.info("executor.escape_por_max_turns node=%s -> %s", node_id, destino)
                if not _move(state, destino, resultado, visitados):
                    break
                continue
            resultado.reason = f"aguardando {missing_required(node, collected, routine)}"
            break

        if isinstance(node, CollectNode):
            if is_filled(collected.get(node.slot)):
                if not _move(state, node.next, resultado, visitados):
                    break
                continue
            resultado.reason = f"aguardando slot {node.slot!r}"
            break

        # --- decide: atravessa ---
        if isinstance(node, DecideNode):
            destino = route_decide(node, state)
            if destino is None:
                resultado.reason = f"decide {node_id!r} sem branch aplicável e sem else"
                logger.warning("executor.decide_sem_saida node=%s", node_id)
                break
            # O sinal é consumido pela decisão que ele destrancou.
            state["last_signal"] = None
            if not _move(state, destino, resultado, visitados):
                break
            continue

        # --- say: renderiza e atravessa ---
        if isinstance(node, SayNode):
            templates.extend(node.templates)
            if not _move(state, node.next, resultado, visitados):
                break
            continue

        # --- tool: o pipeline executa, não o executor ---
        if isinstance(node, ToolNode):
            resultado.pending_tool = node
            resultado.reason = f"tool {node.ref!r} pendente"
            break

        # --- freetalk: fica enquanto conversa; sai quando dá o sinal ---
        if isinstance(node, FreeTalkNode):
            # Autonomia com contrato de saída: o LLM escreve o que quiser
            # dentro do escopo, mas a saída é um dos ``signals`` declarados.
            # Emitido o sinal, o nó cumpriu seu papel e o decide seguinte o
            # consome. Sem esta porta, todo lead sairia por timeout e cairia
            # no mesmo destino, independentemente do que tivesse dito.
            sinal = state.get("last_signal")
            if sinal and sinal in node.signals:
                if not _move(state, node.next, resultado, visitados):
                    break
                continue

            turnos = int(state.get("_freetalk_turn_count", {}).get(node_id, 0))
            if turnos >= node.max_turns:
                destino = node.exit_on_timeout or node.next
                resultado.reason = f"freetalk {node_id!r} estourou {node.max_turns} turnos"
                logger.info("executor.freetalk_timeout node=%s -> %s", node_id, destino)
                if not _move(state, destino, resultado, visitados):
                    break
                continue
            resultado.reason = f"conversando em {node_id!r}"
            break

        # --- call_subroutine: empilha e entra ---
        if isinstance(node, CallSubroutineNode):
            sub = routine.sub_routines.get(node.ref)
            if sub is None:
                resultado.reason = f"sub-rotina {node.ref!r} não existe"
                logger.error("executor.subrotina_inexistente ref=%s", node.ref)
                break
            state.setdefault("subflow_stack", []).append(
                {
                    "ref": node.ref,
                    "return_to": node.next,
                    "return_scope": state.get("scope") or "main",
                }
            )
            state["scope"] = node.ref
            state["current_node"] = sub.start
            state["turns_in_node"] = 0
            resultado.entered_subflow = node.ref
            resultado.moved = True
            continue

        resultado.reason = f"tipo de nó não tratado: {type(node).__name__}"
        logger.error("executor.no_nao_tratado type=%s", type(node).__name__)
        break
    else:
        resultado.reason = f"max_hops={max_hops} atingido — roteiro provavelmente cíclico"
        logger.error("executor.max_hops node=%s", state.get("current_node"))

    # Chegou ao fim de uma sub-rotina? Desempilha e continua no pai.
    if resultado.finished and (state.get("subflow_stack") or []):
        _pop_subflow(state, resultado)
        if not resultado.finished:
            seguinte = advance(routine, state, max_hops=max_hops)
            seguinte.say_templates = templates + seguinte.say_templates
            seguinte.moved = True
            return seguinte

    resultado.node_id = state.get("current_node") or ""
    resultado.say_templates = templates
    return resultado


def end_de_handoff(routine: RoutineAst, state: dict[str, Any]) -> str | None:
    """O ``end`` para onde uma escalada deve pousar.

    Preferência: ``role: handoff`` → ``role: nurture`` → nenhum.

    Um ``handoff_human`` aceito encerra a conversa do ponto de vista do
    agente, mas sem mover o cursor o fluxo fica parado no nó de coleta: o
    canal marca escalada e o runtime acha que ainda está perguntando o nome.

    O v4 resolve isso por palavra-chave no id e no motivo do End, e o próprio
    comentário registra que a heurística é frágil — o adapter de BPMN colapsa
    todos os ``outcome: handed_off`` no mesmo motivo, então só o id distingue
    o End de sucesso do de nutrição. No RoutineAst o ``role`` é declarado, e
    a escolha deixa de ser adivinhação.
    """
    bloco = current_block(routine, state)
    ends = [(nid, n) for nid, n in bloco.nodes.items() if isinstance(n, EndNode)]
    for papel in ("handoff", "nurture"):
        for nid, n in ends:
            if n.role == papel:
                return nid
    return None


def _move(
    state: dict[str, Any],
    destino: str | None,
    resultado: AdvanceResult,
    visitados: set[str] | None = None,
) -> bool:
    """Move o cursor. Devolve ``False`` quando não deve (ou não pode) mover.

    Recusa mover para um nó já pisado neste passo: seria ciclo intra-turno.
    O cursor fica onde está e a palavra volta ao lead.
    """
    if not destino:
        resultado.reason = "nó sem destino declarado"
        return False
    if visitados is not None and destino in visitados:
        resultado.reason = (
            f"ciclo intra-turno: {destino!r} já foi visitado neste passo — "
            "parando aqui para devolver a palavra ao lead"
        )
        logger.info("executor.ciclo_intra_turno destino=%s", destino)
        return False
    state["current_node"] = destino
    state["turns_in_node"] = 0
    resultado.moved = True
    return True


def _pop_subflow(state: dict[str, Any], resultado: AdvanceResult) -> None:
    """Sai da sub-rotina e volta ao nó seguinte no pai."""
    frame = state["subflow_stack"].pop()
    state["scope"] = frame.get("return_scope") or "main"
    volta = frame.get("return_to")
    if volta:
        state["current_node"] = volta
        state["turns_in_node"] = 0
        resultado.finished = False
        resultado.reason = f"sub-rotina {frame.get('ref')!r} concluída, voltando a {volta!r}"
