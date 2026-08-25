"""Canal Telegram — long polling, sem webhook.

Escolha deliberada: o canal Telegram do v4 tem 3.774 linhas de webhook,
assinatura, registro de bots e fila. Para provar o runtime e para uma vitrine,
`getUpdates` num laço resolve — sem HTTPS, sem domínio, sem infraestrutura de
entrada.

`ponytail:` polling não escala para milhares de conversas simultâneas. Se
virar produto, trocar por webhook — a fronteira com o runtime é a mesma
(``WorkflowRuntime.turno``), então a troca não toca o pipeline.

O que esta camada faz, e o runtime não:

- **debounce** — junta a rajada do lead num turno só
- **bolhas** — quebra a resposta em mensagens, com pausa entre elas
- **digitando…** — enquanto o turno roda
- **dedup** — o Telegram reentrega update quando o ack se perde
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

import httpx

from zoi_agno.builder import WorkflowRuntime
from zoi_agno.channel.bubbles import em_bolhas, pausa_de_digitacao
from zoi_agno.channel.buffer import BufferDeEntrada

logger = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{metodo}"


class Dedup:
    """Lembra updates já processados.

    O Telegram reentrega um update quando o ack se perde. Sem isto, uma queda
    de rede no momento errado faz o lead receber a mesma resposta duas vezes.
    """

    def __init__(self, capacidade: int = 2000) -> None:
        self._vistos: OrderedDict[int, None] = OrderedDict()
        self.capacidade = capacidade

    def novo(self, update_id: int) -> bool:
        if update_id in self._vistos:
            return False
        self._vistos[update_id] = None
        while len(self._vistos) > self.capacidade:
            self._vistos.popitem(last=False)
        return True


@dataclass
class ConfigTelegram:
    token: str
    tenant_id: str
    debounce_s: float = 5.0
    pausa_entre_bolhas_s: float = 1.0
    """Pausa mínima; o tamanho da bolha soma a isto."""

    timeout_polling_s: int = 25
    prefixo_sessao: str = "tg"
    saudacao: str = ""
    """Resposta ao ``/start``. Vazio = deixa o agente abrir a conversa."""


@dataclass
class Estatisticas:
    recebidas: int = 0
    turnos: int = 0
    bolhas: int = 0
    erros: int = 0
    handoffs: int = 0
    encerradas: int = 0
    chats: set[str] = field(default_factory=set)


class BotTelegram:
    """Liga um chat do Telegram a um ``WorkflowRuntime``.

    Uma conversa = um ``session_id``, derivado do chat. Todo o estado vive no
    banco do Agno; este objeto não guarda conversa nenhuma.
    """

    def __init__(self, cfg: ConfigTelegram, runtime: WorkflowRuntime) -> None:
        self.cfg = cfg
        self.runtime = runtime
        self.dedup = Dedup()
        self.stats = Estatisticas()
        self.buffer = BufferDeEntrada(self._rodar_turno, segundos=cfg.debounce_s)
        self._http = httpx.AsyncClient(timeout=cfg.timeout_polling_s + 10)
        self._parar = asyncio.Event()

    # -- API do Telegram -------------------------------------------------

    async def _chamar(self, metodo: str, **params: Any) -> dict[str, Any]:
        r = await self._http.post(API.format(token=self.cfg.token, metodo=metodo), json=params)
        r.raise_for_status()
        return r.json()

    async def quem_sou(self) -> dict[str, Any]:
        return (await self._chamar("getMe")).get("result", {})

    async def _digitando(self, chat_id: str) -> None:
        with contextlib.suppress(Exception):  # indicador é enfeite, não pode quebrar o turno
            await self._chamar("sendChatAction", chat_id=chat_id, action="typing")

    async def _enviar(self, chat_id: str, texto: str) -> None:
        await self._chamar("sendMessage", chat_id=chat_id, text=texto)

    # -- o turno ---------------------------------------------------------

    def session_id(self, chat_id: str) -> str:
        return f"{self.cfg.prefixo_sessao}:{chat_id}"

    async def _rodar_turno(self, chat_id: str, texto: str) -> None:
        """Um turno: pensa com "digitando…" ligado, responde em bolhas."""
        digitando = asyncio.create_task(self._manter_digitando(chat_id))
        try:
            turno = await self.runtime.turno(self.session_id(chat_id), texto)
        except Exception:
            self.stats.erros += 1
            logger.exception("telegram.turno_falhou chat=%s", chat_id)
            await self._enviar(chat_id, "Tive um problema aqui. Pode repetir?")
            return
        finally:
            digitando.cancel()

        self.stats.turnos += 1
        if turno.handoff:
            self.stats.handoffs += 1
        if turno.finished:
            self.stats.encerradas += 1

        for i, bolha in enumerate(em_bolhas(turno.texto)):
            if i:
                await asyncio.sleep(self.cfg.pausa_entre_bolhas_s + pausa_de_digitacao(bolha))
                await self._digitando(chat_id)
            await self._enviar(chat_id, bolha)
            self.stats.bolhas += 1

    async def _manter_digitando(self, chat_id: str) -> None:
        """O indicador do Telegram expira em ~5s; renova enquanto o turno roda."""
        try:
            while True:
                await self._digitando(chat_id)
                await asyncio.sleep(4.0)
        except asyncio.CancelledError:
            return

    # -- entrada ---------------------------------------------------------

    async def _processar(self, update: dict[str, Any]) -> None:
        if not self.dedup.novo(int(update.get("update_id", 0))):
            logger.info("telegram.update_repetido id=%s", update.get("update_id"))
            return
        msg = update.get("message") or update.get("edited_message") or {}
        texto = (msg.get("text") or "").strip()
        chat_id = str((msg.get("chat") or {}).get("id") or "")
        if not texto or not chat_id:
            return  # foto, áudio, sticker: fora do escopo desta camada

        self.stats.recebidas += 1
        self.stats.chats.add(chat_id)

        if texto.startswith("/"):
            await self._comando(chat_id, texto)
            return
        await self.buffer.receber(chat_id, texto)

    async def _comando(self, chat_id: str, texto: str) -> None:
        """Comandos do canal. Não passam pelo agente."""
        cmd = texto.split()[0].lower().lstrip("/").split("@")[0]
        if cmd == "start":
            if self.cfg.saudacao:
                await self._enviar(chat_id, self.cfg.saudacao)
            else:
                # Sem saudação fixa, quem abre é o agente — assim a primeira
                # mensagem já é a do roteiro, na voz da persona.
                await self.buffer.receber(chat_id, "oi")
        elif cmd == "reset":
            # Sessão nova: o Agno cria o estado no primeiro turno do id novo.
            self.cfg.prefixo_sessao = f"{self.cfg.prefixo_sessao}!"
            await self._enviar(chat_id, "Conversa reiniciada.")
        elif cmd == "estado":
            st = self.runtime.estado(self.session_id(chat_id))
            slots = {
                k: v for k, v in (st.get("collected") or {}).items() if not isinstance(v, dict)
            }
            await self._enviar(chat_id, f"nó: {st.get('current_node')}\nslots: {slots or '—'}")
        else:
            await self._enviar(chat_id, "Comandos: /start, /reset, /estado")

    # -- laço ------------------------------------------------------------

    async def rodar(self, *, ate_parar: bool = True) -> None:
        """Long polling até ``parar()``."""
        eu = await self.quem_sou()
        logger.info("telegram.iniciado bot=@%s tenant=%s", eu.get("username"), self.cfg.tenant_id)
        offset = 0
        while not self._parar.is_set():
            try:
                r = await self._chamar(
                    "getUpdates", offset=offset, timeout=self.cfg.timeout_polling_s
                )
            except Exception:  # noqa: BLE001 — rede cai; o laço não
                self.stats.erros += 1
                logger.warning("telegram.polling_falhou — tentando de novo em 3s")
                await asyncio.sleep(3.0)
                continue
            for update in r.get("result", []):
                offset = max(offset, int(update.get("update_id", 0)) + 1)
                await self._processar(update)
            if not ate_parar:
                return

    def parar(self) -> None:
        self._parar.set()

    async def fechar(self) -> None:
        await self.buffer.drenar()
        await self._http.aclose()
