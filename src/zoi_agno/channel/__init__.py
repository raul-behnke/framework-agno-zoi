"""A borda de canal — o que fica FORA do runtime.

Debounce, bolhas, indicador de digitação e dedup são propriedades do canal,
não do agente: quem sabe que houve uma rajada de mensagens é quem recebe os
eventos. Manter isso aqui é o que permite trocar Telegram por WhatsApp sem
tocar no pipeline.
"""

from __future__ import annotations

from zoi_agno.channel.bubbles import cortar_no_limite, em_bolhas, pausa_de_digitacao, utf16_len
from zoi_agno.channel.buffer import BufferDeEntrada
from zoi_agno.channel.telegram import BotTelegram, ConfigTelegram, Dedup, Estatisticas

__all__ = [
    "BotTelegram",
    "BufferDeEntrada",
    "ConfigTelegram",
    "Dedup",
    "Estatisticas",
    "cortar_no_limite",
    "em_bolhas",
    "pausa_de_digitacao",
    "utf16_len",
]
