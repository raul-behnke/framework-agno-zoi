"""O crítico de tom — tira o cheiro de IA.

Roda em todo turno voltado ao lead (modo ``always``), com o modelo mais
barato. Julga o rascunho contra três dimensões de voz e, se reprovar, o
redator refaz **uma vez** com o retorno. Fica o melhor dos dois.

Ele não olha fatos nem PII — isso é do guarda de grounding e do Presidio. Aqui
só se avalia se aquilo soa como uma pessoa digitando no WhatsApp.

O que ele pega, e que nenhuma instrução de prompt segura sozinha: registro
corporativo, lista disfarçada de frase, travessão, excesso de reconhecimento
("Perfeito! Ótimo! Que legal!"), e o agente falando de si como sistema.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from typing import Any, Literal

from agno.agent import Agent
from pydantic import BaseModel, Field

from zoi_agno.gateway import modelo_para

logger = logging.getLogger(__name__)

Modo = Literal["always", "conditional", "off"]


class VeredictoTom(BaseModel):
    aprovado: bool = Field(description="Soa como uma pessoa real digitando?")
    retorno: str = Field(
        default="",
        max_length=300,
        description="Se reprovado: o que corrigir, em uma frase acionável.",
    )


@dataclass
class ConfigTom:
    """Como o crítico de tom é acionado.

    ``conditional`` amostra: paga o custo em parte dos turnos e ainda assim
    revela regressão de voz. ``always`` é o default do v4.
    """

    modo: Modo = "always"
    taxa_amostragem: float = 0.10

    def deve_rodar(self, sorteio: float | None = None) -> bool:
        if self.modo == "off":
            return False
        if self.modo == "always":
            return True
        return (sorteio if sorteio is not None else random.random()) < self.taxa_amostragem


INSTRUCOES = [
    (
        "Você julga se um rascunho de mensagem soa como uma PESSOA digitando no "
        "WhatsApp. Você não julga se o conteúdo está certo — só a voz."
    ),
    (
        "Reprove se houver: tom corporativo ou de call center; lista, bullet, "
        "numeração ou travessão; mais de uma pergunta na mesma mensagem; "
        "reconhecimento exagerado ('Perfeito! Ótimo! Que ótimo!'); o agente "
        "falando de si como sistema, fluxo, cadastro ou atendimento automatizado."
    ),
    "Aprove mensagem curta, direta e específica. Brevidade não é defeito.",
    (
        "Ao reprovar, o retorno tem que ser acionável numa frase: o que trocar por "
        "quê. Nunca reescreva a mensagem inteira."
    ),
]


def build(routing: dict[str, Any] | None = None) -> Agent:
    """O crítico de tom usa o papel ``extractor`` — é o tier mais barato."""
    return Agent(
        name="critico_tom",
        model=modelo_para("extractor", routing),
        instructions=INSTRUCOES,
        output_schema=VeredictoTom,
        retries=1,
    )


def montar_entrada(rascunho: str, persona: dict[str, Any]) -> str:
    voz = persona.get("voice") or ""
    proibidas = [
        str(f.get("pattern"))
        for f in (persona.get("forbidden_phrases") or [])
        if isinstance(f, dict) and f.get("pattern") and not f.get("is_regex")
    ][:12]
    partes = [f"VOZ ESPERADA:\n{voz}" if voz else "VOZ ESPERADA: pessoa real no WhatsApp."]
    if proibidas:
        partes.append("EXPRESSÕES PROIBIDAS: " + "; ".join(proibidas))
    partes.append(f"RASCUNHO:\n{rascunho}")
    return "\n\n".join(partes)


async def revisar(agente: Agent, rascunho: str, persona: dict[str, Any]) -> VeredictoTom:
    """Revisa, com fail-soft. Erro vira aprovação — o turno nunca trava."""
    if not rascunho.strip():
        return VeredictoTom(aprovado=True)
    try:
        saida = await agente.arun(montar_entrada(rascunho, persona))
        conteudo = saida.content
        if isinstance(conteudo, VeredictoTom):
            return conteudo
        logger.warning("tone.saida_inesperada tipo=%s", type(conteudo).__name__)
    except Exception as exc:  # noqa: BLE001 — fail-soft por contrato
        logger.warning("tone.falhou err=%r", exc)
    return VeredictoTom(aprovado=True, retorno="fail-soft")
