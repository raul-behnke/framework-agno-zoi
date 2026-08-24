"""O crítico de decisão — verifica o que não dá para desfazer.

Roda **com portão**: só quando o turno carrega uma decisão irreversível —
encaminhar para um humano, encerrar o fluxo, ou apresentar catálogo. Numa
conversa típica isso é raro, então o custo amortizado é quase zero. Turno sem
decisão irreversível não paga chamada nenhuma.

**Fail-soft por construção.** Qualquer erro, timeout ou saída malformada vira
aprovação. A conversa nunca pode travar esperando o crítico — um veto perdido
custa um handoff indevido; um turno travado custa o lead.

O que ele NÃO é: um segundo redator. Ele não reescreve, só aprova ou veta.
Reescrita de voz é trabalho do crítico de tom.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from agno.agent import Agent
from pydantic import BaseModel, Field

from zoi_agno.gateway import modelo_para

logger = logging.getLogger(__name__)

# Comandos cuja aceitação encerra ou desvia a conversa de forma que o próximo
# turno não conserta.
DECISOES_IRREVERSIVEIS = frozenset({"handoff_human", "finish_flow"})


class Veredito(BaseModel):
    """A saída do crítico."""

    aprovado: bool = Field(description="A decisão se sustenta no que a conversa mostra?")
    motivo: str = Field(default="", max_length=300)


@dataclass(frozen=True)
class Portao:
    """Por que o crítico foi (ou não foi) acionado."""

    acionado: bool
    decisao: str = ""


def avaliar_portao(aceitos: list[Any], state: dict[str, Any]) -> Portao:
    """O turno carrega decisão irreversível?

    Função pura: dá para testar o portão inteiro sem modelo nenhum, e é ele
    que determina o custo do crítico na conta do mês.
    """
    for cmd in aceitos:
        kind = cmd.get("kind") if isinstance(cmd, dict) else getattr(cmd, "kind", None)
        if kind in DECISOES_IRREVERSIVEIS:
            return Portao(True, str(kind))
    if state.get("_apresentou_catalogo"):
        return Portao(True, "apresentacao_catalogo")
    return Portao(False)


INSTRUCOES = [
    (
        "Você audita UMA decisão que o agente acabou de tomar numa conversa de "
        "atendimento. Você não conversa com ninguém e não reescreve nada."
    ),
    (
        "Aprove quando a conversa sustenta a decisão. Vete apenas quando ela "
        "contradiz o que o lead disse — não por estilo, não por preferência."
    ),
    (
        "Encaminhar para um humano é quase sempre legítimo: o lead pediu, ou "
        "demonstrou frustração, ou o assunto está fora do escopo do agente. Na "
        "dúvida, aprove."
    ),
    (
        "Encerrar o fluxo exige que o objetivo tenha sido cumprido ou que o lead "
        "tenha desistido claramente. Encerrar no meio de uma coleta é suspeito."
    ),
]


def build(routing: dict[str, Any] | None = None) -> Agent:
    return Agent(
        name="critico",
        model=modelo_para("judge", routing),
        instructions=INSTRUCOES,
        output_schema=Veredito,
        retries=1,
    )


def montar_entrada(decisao: str, state: dict[str, Any], user_msg: str) -> str:
    """O contexto mínimo para julgar a decisão."""
    coletado = {
        k: v
        for k, v in (state.get("collected") or {}).items()
        if not k.startswith("_") and not isinstance(v, dict)
    }
    ultimas = [
        f"{m.get('role')}: {m.get('content')}" for m in (state.get("messages_tail") or [])[-6:]
    ]
    return (
        f"DECISÃO A AUDITAR: {decisao}\n\n"
        f"Última fala do lead: {user_msg!r}\n\n"
        f"Trecho recente da conversa:\n" + "\n".join(ultimas) + "\n\n"
        f"Dados coletados: {json.dumps(coletado, ensure_ascii=False)}\n\n"
        "A decisão se sustenta?"
    )


async def julgar(agente: Agent, decisao: str, state: dict[str, Any], user_msg: str) -> Veredito:
    """Julga, com fail-soft. Erro vira aprovação — a conversa não trava."""
    try:
        saida = await agente.arun(montar_entrada(decisao, state, user_msg))
        conteudo = saida.content
        if isinstance(conteudo, Veredito):
            return conteudo
        logger.warning("critic.saida_inesperada tipo=%s", type(conteudo).__name__)
    except Exception as exc:  # noqa: BLE001 — fail-soft por contrato
        logger.warning("critic.falhou err=%r", exc)
    return Veredito(aprovado=True, motivo="fail-soft")
