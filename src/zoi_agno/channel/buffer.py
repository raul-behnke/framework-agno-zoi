"""Debounce de entrada — gente não escreve tudo de uma vez.

O lead manda "oi", depois "quero cortar o cabelo", depois "hoje dá?". Três
mensagens em oito segundos. Sem debounce o agente responde três vezes e se
atropela: a segunda resposta chega antes de o lead ler a primeira, e o
contexto de cada uma ignora as outras.

O buffer junta a rajada num texto só e roda **um** turno.

Este é o bug mais visível de um agente sem esta camada — e por isso ele fica
no canal, não no runtime: quem sabe que houve rajada é quem recebe os
eventos.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class _Rajada:
    partes: list[str] = field(default_factory=list)
    tarefa: asyncio.Task | None = None


class BufferDeEntrada:
    """Agrupa mensagens seguidas do mesmo chat e dispara um turno só.

    Cada mensagem nova reinicia o relógio: o lead que continua digitando
    continua sendo esperado. O disparo acontece quando ele para.
    """

    def __init__(
        self,
        ao_disparar: Callable[[str, str], Awaitable[None]],
        *,
        segundos: float = 5.0,
        maximo_de_partes: int = 12,
    ) -> None:
        self._ao_disparar = ao_disparar
        self.segundos = segundos
        self.maximo_de_partes = maximo_de_partes
        self._rajadas: dict[str, _Rajada] = {}

    def pendentes(self, chat_id: str) -> int:
        """Quantas mensagens estão esperando. Útil para depurar e testar."""
        r = self._rajadas.get(chat_id)
        return len(r.partes) if r else 0

    async def receber(self, chat_id: str, texto: str) -> None:
        """Adiciona à rajada e reinicia o relógio."""
        r = self._rajadas.setdefault(chat_id, _Rajada())
        r.partes.append(texto)
        if r.tarefa is not None:
            r.tarefa.cancel()

        # Rajada longa demais dispara na hora: o lead está desabafando, e
        # esperar mais só aumenta o silêncio do outro lado.
        if len(r.partes) >= self.maximo_de_partes:
            await self._disparar(chat_id)
            return

        r.tarefa = asyncio.create_task(self._esperar_e_disparar(chat_id))

    async def _esperar_e_disparar(self, chat_id: str) -> None:
        try:
            await asyncio.sleep(self.segundos)
        except asyncio.CancelledError:
            return  # chegou mensagem nova; outro relógio assumiu
        await self._disparar(chat_id)

    async def _disparar(self, chat_id: str) -> None:
        r = self._rajadas.pop(chat_id, None)
        if r is None or not r.partes:
            return
        # Cancelar o relógio, MENOS quando é ele mesmo quem está disparando:
        # a task se cancelaria, e o cancelamento efetivaria no próximo await —
        # que é a pausa entre bolhas. Resultado: só a primeira bolha saía, e
        # em silêncio. Toda resposta de duas ou mais mensagens ficava pela
        # metade.
        atual = asyncio.current_task()
        if r.tarefa is not None and r.tarefa is not atual:
            r.tarefa.cancel()
        texto = "\n".join(r.partes)
        if len(r.partes) > 1:
            logger.info("buffer.rajada chat=%s partes=%d", chat_id, len(r.partes))
        try:
            await self._ao_disparar(chat_id, texto)
        except Exception:
            logger.exception("buffer.turno_falhou chat=%s", chat_id)

    async def drenar(self) -> None:
        """Dispara tudo que está esperando. Para desligar sem perder mensagem."""
        for chat_id in list(self._rajadas):
            await self._disparar(chat_id)
