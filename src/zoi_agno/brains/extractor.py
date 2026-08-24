"""O extrator — traduz a fala do lead em comandos.

Não decide nada, não escreve nada. Lê o que o lead disse e emite comandos de
uma lista fechada. É o cérebro mais barato do pipeline e o que roda em todo
turno, por isso usa o papel ``extractor`` do ``routing.yaml``.

Duas propriedades que o prompt precisa garantir, ambas nascidas de bug real:

**Multi-extração.** Se o lead diz "sou a Ana, de Curitiba, quero corte", os
três slots saem num turno só. O contrário — um slot por turno — é o que faz o
agente parecer um formulário.

**Nada além do que foi dito.** O extrator não infere, não completa, não
"ajuda". Um valor inventado vira estado, e estado errado roteia o fluxo todo
para o lugar errado. Quando não tem certeza, ele emite ``clarify`` ou nada.
"""

from __future__ import annotations

import logging
from typing import Any

from agno.agent import Agent
from zoi_routine.ast import CollectGroupNode, CollectNode, FreeTalkNode

from zoi_agno.contracts import CommandGenOutput
from zoi_agno.gateway import modelo_para

logger = logging.getLogger(__name__)

INSTRUCOES = [
    "Você extrai COMANDOS da última mensagem do lead. Você não conversa com ele.",
    (
        "Emita apenas o que o lead REALMENTE disse nesta mensagem. Nunca infira, "
        "nunca complete, nunca 'ajude' preenchendo o que faltou."
    ),
    (
        "Se o lead respondeu várias coisas de uma vez, emita um set_slot para cada "
        "uma — é normal e desejável extrair 3 ou 4 slots num turno."
    ),
    (
        "Se o lead fez uma PERGUNTA, isso não é resposta: não vire set_slot. "
        "Se precisar, emita clarify."
    ),
    (
        "Se o lead pediu para falar com uma pessoa, emita handoff_human — sempre, "
        "sem exceção, mesmo que o fluxo pareça incompleto."
    ),
    (
        "Slot com valores declarados (enum): use EXATAMENTE um dos valores da "
        "lista, nunca a fala do lead verbatim. Se o que ele disse corresponde a "
        "mais de um valor, escolha o que representa o conjunto — a fiscalização "
        "descarta valor ambíguo em vez de adivinhar, e o dado se perde."
    ),
    (
        "Se não houver nada a extrair, devolva a lista vazia. Lista vazia é uma "
        "resposta correta e comum."
    ),
    (
        "confidence reflete o quanto o texto do lead sustenta o valor: 1.0 quando "
        "ele disse literalmente; abaixo de 0.7 quando você está interpretando."
    ),
]


def _contexto_do_no(node: Any, routine: Any) -> str:
    """O que o extrator precisa saber sobre onde a conversa está.

    Só o nó ativo — dar o grafo inteiro convida o modelo a preencher slots de
    etapas futuras, que é exatamente o que a rule ``slot_scope`` derruba.
    """
    if isinstance(node, CollectGroupNode):
        linhas = [f"O agente está coletando o grupo {node.group_name!r}. Campos:"]
        for f in node.fields:
            decl = routine.slots.get(f.name)
            tipo = (
                f"enum{list(decl.values)}" if decl and decl.values else (decl.type if decl else "?")
            )
            obrig = " (obrigatório)" if f.required else ""
            linhas.append(f"  - {f.name}: {tipo}{obrig}")
        return "\n".join(linhas)
    if isinstance(node, CollectNode):
        decl = routine.slots.get(node.slot)
        tipo = f"enum{list(decl.values)}" if decl and decl.values else (decl.type if decl else "?")
        return f"O agente está coletando o slot {node.slot!r} ({tipo}). Pergunta: {node.question!r}"
    if isinstance(node, FreeTalkNode):
        partes = [f"O agente está em conversa livre. Escopo: {node.scope or node.goal or ''}"]
        if node.signals:
            partes.append(
                "Sinais que você PODE emitir (só estes, nome exato): " + ", ".join(node.signals)
            )
        if node.slots:
            partes.append("Slots capturáveis aqui: " + ", ".join(node.slots))
        return "\n".join(partes)
    return f"Nó atual do tipo {type(node).__name__}."


def build(routing: dict[str, Any] | None = None, db: Any = None) -> Agent:
    """O agente extrator, com saída estruturada nos 15 comandos."""
    return Agent(
        name="extrator",
        model=modelo_para("extractor", routing),
        instructions=INSTRUCOES,
        output_schema=CommandGenOutput,
        db=db,
        add_history_to_context=bool(db),
        num_history_runs=6,
        retries=2,
    )


def montar_entrada(user_msg: str, node: Any, routine: Any, collected: dict[str, Any]) -> str:
    """A mensagem que o extrator recebe.

    ``collected`` entra para que ele não re-extraia o que já está preenchido,
    e para que perceba correção ("na verdade é Curitiba, não São Paulo").
    """
    ja = ", ".join(f"{k}={v!r}" for k, v in collected.items() if not k.startswith("_")) or "nada"
    return (
        f"{_contexto_do_no(node, routine)}\n\n"
        f"Já coletado nesta conversa: {ja}\n\n"
        f"Mensagem do lead: {user_msg!r}"
    )
