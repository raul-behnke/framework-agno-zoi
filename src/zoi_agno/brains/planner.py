"""O planejador — enxerga alguns passos à frente.

Ele **não move o cursor**. Isso é do executor, e é determinístico. O plano
serve a duas coisas mais modestas, mas reais:

**Antecipação no prompt do redator.** Sabendo que depois desta coleta vem uma
busca e depois uma apresentação, o redator conduz a conversa em vez de marchar
pergunta a pergunta.

**Contabilidade de desvio.** Quando o cursor cai fora dos alvos do plano por
turnos seguidos, algo está errado — o lead levou a conversa para outro lugar,
ou o roteiro tem um buraco. O contador ``_drift_streak`` torna isso visível.

Falha vira ``None`` e o turno segue em modo reativo. Um plano ausente degrada
a naturalidade; um plano errado degrada o fluxo. Por isso o planner é o único
cérebro que prefere não responder a responder mal.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from agno.agent import Agent
from pydantic import BaseModel, Field
from zoi_routine.ast import RoutineAst

from zoi_agno.executor import current_block
from zoi_agno.gateway import modelo_para

logger = logging.getLogger(__name__)

Intencao = Literal["collect_slot", "ask_group", "decide", "say", "subflow", "freetalk", "end"]

# O planner tende a devolver o TIPO DO NÓ no lugar da intenção. Todos mapeiam
# de forma determinística, então coagimos antes de validar em vez de descartar
# o plano inteiro e cair no modo reativo.
_COERCOES: dict[str, Intencao] = {
    "collect_group": "ask_group",
    "collectgroup": "ask_group",
    "collect": "collect_slot",
    "tool": "subflow",
    "call_subroutine": "subflow",
    "callsubroutine": "subflow",
    "subroutine": "subflow",
}


class PassoDoPlano(BaseModel):
    intencao: Intencao
    alvo: str = Field(description="id do nó")
    porque: str = Field(default="", max_length=150)


class Plano(BaseModel):
    passos: list[PassoDoPlano] = Field(default_factory=list, max_length=8)


INSTRUCOES = [
    (
        "Você projeta os próximos passos de uma conversa de atendimento que segue "
        "um roteiro. Você não escreve mensagem e não fala com o lead."
    ),
    "Use SOMENTE ids de nó da lista de alcançáveis. Nó que não está na lista não existe para você.",
    (
        "Projete até onde a conversa provavelmente chega neste e nos próximos "
        "turnos — não o roteiro inteiro."
    ),
    (
        "Se o lead levou a conversa para outro lugar, o plano deve refletir para "
        "onde ELE está indo, não para onde o roteiro gostaria."
    ),
]


def build(routing: dict[str, Any] | None = None) -> Agent:
    return Agent(
        name="planner",
        model=modelo_para("agent", routing),
        instructions=INSTRUCOES,
        output_schema=Plano,
        retries=1,
    )


def nos_alcancaveis(routine: RoutineAst, state: dict[str, Any], *, limite: int = 30) -> list[str]:
    """Ids do escopo ativo. É a lista fechada que o planner pode citar."""
    return list(current_block(routine, state).nodes.keys())[:limite]


def montar_entrada(
    routine: RoutineAst, state: dict[str, Any], user_msg: str, flow_goal: str | None = None
) -> str:
    alcancaveis = nos_alcancaveis(routine, state)
    coletado = {
        k: v
        for k, v in (state.get("collected") or {}).items()
        if not k.startswith("_") and not isinstance(v, dict)
    }
    partes = []
    if flow_goal:
        partes.append(f"OBJETIVO DO FLUXO:\n{flow_goal}")
    partes += [
        f"NÓ ATUAL: {state.get('current_node')}",
        f"NÓS ALCANÇÁVEIS: {', '.join(alcancaveis)}",
        f"JÁ COLETADO: {coletado or 'nada'}",
        f"ÚLTIMA FALA DO LEAD: {user_msg!r}",
    ]
    return "\n\n".join(partes)


async def planejar(
    agente: Agent, routine: RoutineAst, state: dict[str, Any], user_msg: str
) -> Plano | None:
    """Devolve o plano, ou ``None`` para o turno seguir em modo reativo."""
    try:
        saida = await agente.arun(montar_entrada(routine, state, user_msg, routine.flow_goal))
    except Exception as exc:  # noqa: BLE001
        logger.warning("planner.falhou err=%r", exc)
        return None

    plano = saida.content
    if not isinstance(plano, Plano):
        logger.warning("planner.saida_inesperada tipo=%s", type(plano).__name__)
        return None

    # Passos para nós inexistentes só incham o prompt do redator e podem
    # mandá-lo antecipar um ramo morto. Descarta em vez de confiar.
    validos = set(nos_alcancaveis(routine, state))
    mantidos = [p for p in plano.passos if p.alvo in validos]
    if len(mantidos) != len(plano.passos):
        logger.info(
            "planner.passos_descartados n=%d alvos=%s",
            len(plano.passos) - len(mantidos),
            [p.alvo for p in plano.passos if p.alvo not in validos],
        )
    if not mantidos:
        return None
    plano.passos = mantidos
    return plano


def coagir_intencoes(bruto: dict[str, Any]) -> dict[str, Any]:
    """Mapeia tipo-de-nó para intenção antes da validação.

    O modelo escreve ``collect_group`` onde o schema espera ``ask_group``. São
    equivalentes; falhar por isso jogaria fora um plano inteiro.
    """
    for passo in bruto.get("passos") or []:
        if isinstance(passo, dict):
            atual = str(passo.get("intencao", "")).strip().lower()
            if mapeado := _COERCOES.get(atual):
                passo["intencao"] = mapeado
    return bruto


def atualizar_drift(state: dict[str, Any], plano: Plano | None) -> int:
    """Conta turnos seguidos em que o cursor caiu fora do plano.

    Determinístico, sem LLM. Um streak alto é sinal de que o roteiro e a
    conversa real divergiram — dado de operação, não gatilho automático.
    """
    if plano is None or not plano.passos:
        return int(state.get("_drift_streak", 0))
    alvos = {p.alvo for p in plano.passos}
    if state.get("current_node") in alvos:
        state["_drift_streak"] = 0
    else:
        state["_drift_streak"] = int(state.get("_drift_streak", 0)) + 1
    return state["_drift_streak"]
