"""O redator — quem escreve a mensagem que o lead lê.

Recebe a persona do tenant, a posição no fluxo e o **grounding**: a lista
fechada de fatos que ele pode citar. Tudo que não estiver ali é invenção, e o
guarda pós-geração derruba.

O que ele NÃO faz: decidir para onde a conversa vai. O cursor já se moveu
antes dele. Ele redige a pergunta do nó em que o executor parou.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from agno.agent import Agent
from zoi_routine.ast import CollectGroupNode, CollectNode, DecideNode, EndNode, FreeTalkNode

from zoi_agno.gateway import modelo_para
from zoi_agno.prompts import (
    HIERARQUIA_DE_INSTRUCAO,
    contexto_de_fluxo,
    linha_de_hoje,
    montar_grounding,
    render_grounding,
)

logger = logging.getLogger(__name__)

BASE = [
    (
        "Você é um atendente humano no WhatsApp. Escreva como uma pessoa digitando: "
        "frases curtas, uma ideia por mensagem."
    ),
    "Nunca use markdown, bullet, lista numerada ou travessão. É conversa, não e-mail.",
    (
        "Reaja ao que o lead acabou de dizer ANTES de perguntar a próxima coisa — "
        "uma oração curta reconhecendo, e só então a pergunta."
    ),
    "Nunca repita uma pergunta que o lead já respondeu.",
    "Nunca revele que é uma IA, nem mencione fluxo, etapa, sistema ou cadastro.",
    HIERARQUIA_DE_INSTRUCAO,
    (
        "Só afirme fatos que estejam no contexto factual fornecido. Se não está "
        "lá, você não sabe — e dizer que vai confirmar é melhor que inventar."
    ),
]


def build(persona: dict[str, Any], routing: dict[str, Any] | None = None, db: Any = None) -> Agent:
    """O redator do tenant: persona + regras de voz + few-shots."""
    instrucoes = list(BASE)
    if mission := persona.get("mission"):
        instrucoes.insert(0, f"Quem você é:\n{mission}")
    if voice := persona.get("voice"):
        instrucoes.append(f"Sua voz:\n{voice}")
    if proibidas := _frases_proibidas(persona):
        instrucoes.append("NUNCA escreva estas expressões: " + "; ".join(proibidas))

    return Agent(
        name=f"redator:{persona.get('name', 'agente')}",
        model=modelo_para("agent", routing),
        instructions=instrucoes,
        additional_input=_few_shots(persona),
        db=db,
        add_history_to_context=bool(db),
        num_history_runs=8,
        retries=2,
    )


def _frases_proibidas(persona: dict[str, Any]) -> list[str]:
    """Extrai só os padrões literais — os regex vão para o guarda determinístico."""
    saida: list[str] = []
    for f in persona.get("forbidden_phrases") or []:
        if isinstance(f, dict) and not f.get("is_regex") and f.get("pattern"):
            saida.append(str(f["pattern"]))
    return saida[:20]


def _few_shots(persona: dict[str, Any]) -> list[dict[str, str]]:
    """Diálogos de exemplo da persona, no formato de histórico do Agno."""
    msgs: list[dict[str, str]] = []
    for ex in (persona.get("fewshot_examples") or [])[:12]:
        if isinstance(ex, dict) and ex.get("user") and ex.get("agent"):
            msgs.append({"role": "user", "content": str(ex["user"])})
            msgs.append({"role": "assistant", "content": str(ex["agent"])})
    return msgs


# --------------------------------------------------------------------------
# grounding — o que o redator pode dizer
# --------------------------------------------------------------------------


def build_grounding(state: dict[str, Any], node: Any) -> dict[str, Any]:
    """Os fatos citáveis neste turno.

    Deliberadamente estreito: o que a tool devolveu e o que o lead já disse.
    Nada de "o modelo sabe sobre o assunto" — se não veio do dado, não existe.

    Slot e payload aparecem UMA vez cada. Antes, ``collected`` inteiro ia num
    campo e os payloads iam de novo em outro — o mesmo blob de candidatos
    duas vezes no mesmo prompt.
    """
    fluxo = contexto_de_fluxo(state)
    g = montar_grounding(state)
    fatos: dict[str, Any] = {"coletado": fluxo.slots}
    if fluxo.confirmacoes_pendentes:
        fatos["aguardando_confirmacao"] = fluxo.confirmacoes_pendentes
    fatos.update(g.payloads)
    return fatos


def montar_entrada(
    *,
    user_msg: str,
    node: Any,
    routine: Any,
    state: dict[str, Any],
    say_templates: list[str] | None = None,
    handoff: bool = False,
) -> str:
    """A instrução de turno do redator: o que dizer agora, e com que material."""
    grounding = build_grounding(state, node)
    partes = [
        linha_de_hoje(),
        "",
        f"Mensagem do lead: {user_msg!r}" if user_msg else "O lead ainda não disse nada.",
        "",
        "CONTEXTO FACTUAL — só o que está aqui pode ser afirmado:",
        json.dumps(grounding, ensure_ascii=False, indent=2, default=str),
        "",
        _tarefa_handoff(state) if handoff else _tarefa(node, routine, state),
    ]
    if say_templates:
        partes += [
            "",
            "Mensagem já definida pelo roteiro (use como base, ajuste o tom):",
            *say_templates,
        ]
    if passos := _proximos_passos(state):
        partes += [
            "",
            (
                f"PARA ONDE A CONVERSA VAI: {passos}. "
                "Conduza nessa direção — não anuncie etapas, não descreva o processo."
            ),
        ]
    # Por último: é a posição mais saliente do prompt, e é onde ficam a
    # proibição de citar catálogo inexistente e a disclosure de relaxamento.
    if bloco := render_grounding(montar_grounding(state), "redator"):
        partes += ["", bloco]
    return "\n".join(partes)


def _proximos_passos(state: dict[str, Any], limite: int = 3) -> str:
    """O plano em uma linha, para o redator conduzir em vez de marchar.

    Só o "porquê" de cada passo — id de nó não diz nada ao redator e ainda
    convida o modelo a mencionar o mecanismo para o lead.
    """
    passos = (state.get("plan") or {}).get("passos") or []
    motivos = [str(p.get("porque") or "").strip() for p in passos[:limite]]
    return "; ".join(m for m in motivos if m)


def _tarefa_handoff(state: dict[str, Any]) -> str:
    """Quando o turno decidiu encaminhar, o texto tem que dizer isso.

    Sem esta porta o redator segue o roteiro normalmente e oferece opções,
    enquanto o canal já marcou a conversa como escalada — o lead lê uma coisa
    e o sistema faz outra.
    """
    motivo = state.get("_handoff_reason") or "o lead pediu"
    return (
        "TAREFA: este turno ESCALA a conversa para uma pessoa "
        f"(motivo: {motivo}). Confirme que vai encaminhar, de forma breve e "
        "cordial. NÃO ofereça opções, NÃO faça pergunta de avanço, NÃO tente "
        "resolver por conta própria."
    )


def _tarefa(node: Any, routine: Any, state: dict[str, Any]) -> str:
    """O que este nó específico pede do redator."""
    collected = state.get("collected") or {}

    if isinstance(node, CollectGroupNode):
        faltando = [f for f in node.fields if not collected.get(f.name) and (f.question or f.name)]
        if not faltando:
            return "Confirme o que foi entendido e siga a conversa naturalmente."
        perguntas = [f.question for f in faltando if f.question] or [
            f"descubra {f.name}" for f in faltando
        ]
        if len(perguntas) == 1:
            return f"TAREFA: faça esta pergunta, com as suas palavras: {perguntas[0]!r}"
        return (
            "TAREFA: descubra estas coisas. Combine numa pergunta natural se der, "
            "nunca despeje uma lista:\n" + "\n".join(f"  - {p}" for p in perguntas)
        )

    if isinstance(node, CollectNode):
        return f"TAREFA: faça esta pergunta, com as suas palavras: {node.question!r}"

    if isinstance(node, FreeTalkNode):
        return f"TAREFA (conversa livre, dentro deste escopo):\n{node.goal or node.scope}"

    if isinstance(node, EndNode):
        if node.farewell:
            return f"TAREFA: despeça-se. Base: {node.farewell!r}"
        return "TAREFA: encerre a conversa com cordialidade."

    # O cursor não deveria descansar num decide (ver `advance._estacionar`),
    # mas se descansar, a tarefa genérica é perigosa: sem pergunta e sem
    # escopo, o redator lê o plano e improvisa a pergunta do nó que ele ACHA
    # que vem a seguir — inclusive pedindo dado de uma etapa que a conversa
    # ainda não alcançou.
    if isinstance(node, DecideNode):
        return (
            "TAREFA: o fluxo não conseguiu avançar — falta alguma coisa que o "
            "lead ainda não disse com clareza. Retome o ponto anterior da "
            "conversa e peça a confirmação que falta, com as suas palavras. "
            "NÃO puxe assunto novo e NÃO peça dado de uma etapa seguinte."
        )

    return "TAREFA: responda ao lead de forma natural e avance a conversa."
