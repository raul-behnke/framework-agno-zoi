"""zoi-agno — o modelo de composição de agentes da ZOI, sobre Agno.

A ideia central: **o LLM interpreta e redige; o código decide e executa.**

O fluxo da conversa vive num ``routine.yaml`` versionado, não num prompt. O
LLM lê a fala do lead e emite *comandos* de uma lista fechada; a fiscalização
decide quais viram realidade; um executor determinístico move o cursor do
grafo; e só então um redator escreve a resposta, com o que ele pode dizer
limitado ao que os dados sustentam.

Consulte ``PLANO-zoi-agno-telegram.md`` para as decisões de arquitetura.
"""

from __future__ import annotations

__version__ = "0.1.0"
