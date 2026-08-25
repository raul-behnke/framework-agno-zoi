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
from zoi_agno.prompts import HIERARQUIA_DE_INSTRUCAO, contexto_de_fluxo, linha_de_hoje

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
    HIERARQUIA_DE_INSTRUCAO,
]


def _tipo_do_slot(nome: str, routine: Any) -> str:
    decl = routine.slots.get(nome)
    if decl is None:
        return "?"
    return f"enum{list(decl.values)}" if decl.values else str(decl.type)


def manifesto_de_slots(routine: Any, node: Any) -> str:
    """Todos os slots declarados, marcando os que estão sendo perguntados.

    O extrator precisa do manifesto inteiro, não só do grupo ativo — o lead
    responde na ordem dele, não na do roteiro.

    Falha real: o lead disse "sou o Marcos, consigo falar sim" enquanto o nó
    ativo coletava só disponibilidade. Sem ver que ``nome`` existe, o extrator
    descartou o nome, e o agente perguntou de novo nos dois turnos seguintes —
    contra a instrução explícita da persona de nunca repetir pergunta já
    respondida.

    Isto não afrouxa a contenção: quem decide o que vira estado é a rule
    ``slot_scope``, que já aceita qualquer slot declarado no fluxo. O que
    mudou é o extrator saber que eles existem.
    """
    if not routine.slots:
        return ""
    ativos: set[str] = set()
    if isinstance(node, CollectGroupNode):
        ativos = {f.name for f in node.fields}
    elif isinstance(node, CollectNode):
        ativos = {node.slot}
    elif isinstance(node, FreeTalkNode):
        ativos = set(node.slots or [])

    linhas = ["Slots declarados neste fluxo (▶ = o que o agente está perguntando agora):"]
    for nome in routine.slots:
        marca = "▶" if nome in ativos else " "
        linhas.append(f"  {marca} {nome}: {_tipo_do_slot(nome, routine)}")
    linhas.append(
        "Se o lead informar espontaneamente um slot que não está marcado, CAPTURE "
        "assim mesmo — ele responde na ordem dele, não na do roteiro."
    )
    return "\n".join(linhas)


def _contexto_do_no(node: Any, routine: Any) -> str:
    """O que o agente está perguntando agora."""
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
            # Nó de ESCOLHA: as opções já foram apresentadas ao lead a partir
            # do estado, então a resposta dele é um pick — não uma pergunta.
            #
            # Bug real do v4, portado com a diretiva: o lead escolhia "8h30", o
            # extrator ficava em loop pedindo mais contexto, o slot nunca era
            # setado, o decide seguinte nunca roteava e o agendamento nunca
            # disparava. O par set_slot + signal tem que sair JUNTO.
            partes += [
                f"Este nó CAPTURA: {', '.join(node.slots)}.",
                "As opções já foram apresentadas ao lead. Quando ele escolher uma:",
                (
                    "  - emita set_slot com o valor EXATO da opção como ela aparece "
                    "no estado (o id, não a paráfrase nem o rótulo legível);"
                ),
                (
                    "  - emita TAMBÉM o signal correspondente — o primeiro declarado "
                    "para escolha bem-sucedida, ou o apropriado se ele recusar tudo "
                    "ou desistir."
                ),
                (
                    "Escolha clara do lead resolve o turno: não peça confirmação do "
                    "que ele acabou de escolher."
                ),
            ]
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
    fluxo = contexto_de_fluxo({"collected": collected})
    ja = ", ".join(f"{k}={v!r}" for k, v in fluxo.slots.items()) or "nada"
    partes = [linha_de_hoje(), _contexto_do_no(node, routine)]
    if manifesto := manifesto_de_slots(routine, node):
        partes.append(manifesto)
    partes += [f"Já coletado nesta conversa: {ja}", f"Mensagem do lead: {user_msg!r}"]
    return "\n\n".join(partes)
