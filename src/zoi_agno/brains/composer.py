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
from zoi_routine.ast import CollectGroupNode, CollectNode, EndNode, FreeTalkNode

from zoi_agno.gateway import modelo_para

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
    Nada de "o modelo sabe sobre carros" — se não veio do dado, não existe.
    """
    collected = state.get("collected") or {}
    fatos: dict[str, Any] = {
        "coletado": {k: v for k, v in collected.items() if not k.startswith("_")},
    }
    for chave, valor in collected.items():
        if isinstance(valor, dict) and ("candidates" in valor or "slots" in valor):
            fatos[chave] = valor
    return fatos


def montar_entrada(
    *,
    user_msg: str,
    node: Any,
    routine: Any,
    state: dict[str, Any],
    say_templates: list[str] | None = None,
) -> str:
    """A instrução de turno do redator: o que dizer agora, e com que material."""
    grounding = build_grounding(state, node)
    partes = [
        f"Mensagem do lead: {user_msg!r}" if user_msg else "O lead ainda não disse nada.",
        "",
        "CONTEXTO FACTUAL — só o que está aqui pode ser afirmado:",
        json.dumps(grounding, ensure_ascii=False, indent=2, default=str),
        "",
        _tarefa(node, routine, state),
    ]
    if say_templates:
        partes += [
            "",
            "Mensagem já definida pelo roteiro (use como base, ajuste o tom):",
            *say_templates,
        ]
    if rejeicoes := state.get("enforcement_rejections"):
        motivos = "; ".join(str(r.get("code")) for r in rejeicoes[:4])
        partes += [
            "",
            (
                f"Neste turno o sistema descartou informação por: {motivos}. "
                "Se algo ficou ambíguo, pergunte de novo com naturalidade."
            ),
        ]
    return "\n".join(partes)


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

    return "TAREFA: responda ao lead de forma natural e avance a conversa."
