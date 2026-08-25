"""A borda de canal — debounce, bolhas, dedup.

Nada aqui fala com o Telegram: o cliente HTTP é substituído. O que se testa é
o comportamento que separa "bot" de "pessoa digitando", e que vive fora do
runtime de propósito.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from zoi_agno.channel import BufferDeEntrada, Dedup, cortar_no_limite, em_bolhas, utf16_len
from zoi_agno.channel.bubbles import MAX_UTF16, pausa_de_digitacao
from zoi_agno.channel.telegram import BotTelegram, ConfigTelegram

# --------------------------------------------------------------------------
# bolhas
# --------------------------------------------------------------------------


def test_paragrafos_viram_bolhas() -> None:
    assert em_bolhas("Oi, tudo bem?\n\nComo é seu nome?") == ["Oi, tudo bem?", "Como é seu nome?"]


def test_sem_paragrafo_quebra_por_linha() -> None:
    """É assim que o redator separa ideias."""
    assert em_bolhas("Perfeito, Ana.\nQual horário você prefere?") == [
        "Perfeito, Ana.",
        "Qual horário você prefere?",
    ]


def test_linha_curta_nao_vira_bolha_solta() -> None:
    """ "Show!" sozinho numa mensagem parece tique, não conversa."""
    assert em_bolhas("Show!\nQual dia fica melhor pra você?") == [
        "Show!\nQual dia fica melhor pra você?"
    ]


def test_texto_unico_continua_uma_bolha() -> None:
    assert em_bolhas("Fechado, te espero!") == ["Fechado, te espero!"]


def test_texto_vazio_nao_gera_bolha() -> None:
    assert em_bolhas("") == []
    assert em_bolhas("   \n  ") == []


def test_excedente_vai_para_a_ultima_bolha() -> None:
    """Oito mensagens seguidas é metralhadora, não conversa."""
    texto = "\n\n".join(f"Ideia número {i} que é razoavelmente longa" for i in range(8))
    bolhas = em_bolhas(texto, max_bolhas=3)
    assert len(bolhas) == 3
    assert "Ideia número 7" in bolhas[-1]


def test_emoji_conta_duas_unidades_utf16() -> None:
    """Contagem ingênua por caractere trunca mensagem com emoji."""
    assert utf16_len("oi") == 2
    assert utf16_len("👊") == 2
    assert utf16_len("oi 👊") == 5


def test_corte_no_limite_nao_parte_emoji() -> None:
    texto = "👊" * (MAX_UTF16 // 2 + 10)
    partes = cortar_no_limite(texto)
    assert len(partes) > 1
    for p in partes:
        assert utf16_len(p) <= MAX_UTF16
    assert "".join(partes) == texto, "nenhum caractere pode se perder no corte"


def test_pausa_cresce_com_o_tamanho_mas_tem_teto() -> None:
    curta = pausa_de_digitacao("oi")
    longa = pausa_de_digitacao("x" * 5000)
    assert curta < longa
    assert longa <= 3.5


# --------------------------------------------------------------------------
# debounce
# --------------------------------------------------------------------------


async def test_rajada_vira_um_turno_so() -> None:
    """O bug mais visível de um agente sem esta camada."""
    disparos: list[tuple[str, str]] = []

    async def ao_disparar(chat: str, texto: str) -> None:
        disparos.append((chat, texto))

    buf = BufferDeEntrada(ao_disparar, segundos=0.05)
    await buf.receber("c1", "oi")
    await buf.receber("c1", "quero cortar o cabelo")
    await buf.receber("c1", "hoje dá?")
    await asyncio.sleep(0.2)

    assert len(disparos) == 1, "três mensagens deviam virar um turno"
    assert disparos[0][1] == "oi\nquero cortar o cabelo\nhoje dá?"


async def test_mensagem_nova_reinicia_o_relogio() -> None:
    """Lead que continua digitando continua sendo esperado."""
    disparos: list[str] = []

    async def ao_disparar(_c: str, texto: str) -> None:
        disparos.append(texto)

    buf = BufferDeEntrada(ao_disparar, segundos=0.15)
    await buf.receber("c1", "um")
    await asyncio.sleep(0.10)
    await buf.receber("c1", "dois")
    await asyncio.sleep(0.10)
    assert disparos == [], "ainda não parou de digitar"
    await asyncio.sleep(0.15)
    assert disparos == ["um\ndois"]


async def test_chats_diferentes_nao_se_misturam() -> None:
    disparos: dict[str, str] = {}

    async def ao_disparar(chat: str, texto: str) -> None:
        disparos[chat] = texto

    buf = BufferDeEntrada(ao_disparar, segundos=0.05)
    await buf.receber("c1", "sou a Ana")
    await buf.receber("c2", "sou o Bruno")
    await asyncio.sleep(0.2)
    assert disparos == {"c1": "sou a Ana", "c2": "sou o Bruno"}


async def test_rajada_muito_longa_dispara_na_hora() -> None:
    """Lead desabafando: esperar mais só aumenta o silêncio."""
    disparos: list[str] = []

    async def ao_disparar(_c: str, texto: str) -> None:
        disparos.append(texto)

    buf = BufferDeEntrada(ao_disparar, segundos=30.0, maximo_de_partes=3)
    for i in range(3):
        await buf.receber("c1", f"msg {i}")
    assert len(disparos) == 1, "não devia esperar os 30s"


async def test_turno_que_quebra_nao_derruba_o_buffer() -> None:
    async def explode(_c: str, _t: str) -> None:
        raise RuntimeError("boom")

    buf = BufferDeEntrada(explode, segundos=0.05)
    await buf.receber("c1", "oi")
    await asyncio.sleep(0.15)
    await buf.receber("c1", "de novo")  # o buffer continua vivo
    await asyncio.sleep(0.15)


async def test_drenar_dispara_o_que_esta_esperando() -> None:
    """Desligar sem perder mensagem."""
    disparos: list[str] = []

    async def ao_disparar(_c: str, texto: str) -> None:
        disparos.append(texto)

    buf = BufferDeEntrada(ao_disparar, segundos=30.0)
    await buf.receber("c1", "não me perca")
    await buf.drenar()
    assert disparos == ["não me perca"]


# --------------------------------------------------------------------------
# dedup
# --------------------------------------------------------------------------


def test_update_repetido_e_ignorado() -> None:
    """O Telegram reentrega quando o ack se perde."""
    d = Dedup()
    assert d.novo(1) is True
    assert d.novo(1) is False
    assert d.novo(2) is True


def test_dedup_esquece_os_mais_antigos() -> None:
    d = Dedup(capacidade=3)
    for i in range(5):
        d.novo(i)
    assert d.novo(0) is True, "saiu da janela"
    assert d.novo(4) is False, "ainda na janela"


# --------------------------------------------------------------------------
# bot
# --------------------------------------------------------------------------


class RuntimeFalso:
    def __init__(self, texto: str = "resposta") -> None:
        self.texto = texto
        self.chamadas: list[tuple[str, str]] = []
        self.estados: dict[str, dict[str, Any]] = {}

    async def turno(self, session_id: str, user_msg: str):
        from zoi_agno.pipeline import Turno

        self.chamadas.append((session_id, user_msg))
        return Turno(texto=self.texto, node_id="n", finished=False, handoff=False)

    def estado(self, session_id: str) -> dict[str, Any]:
        return self.estados.get(session_id, {"current_node": "c_um", "collected": {}})


@pytest.fixture
def bot():
    rt = RuntimeFalso("Oi!\n\nComo é seu nome?")
    b = BotTelegram(ConfigTelegram(token="x", tenant_id="t", debounce_s=0.05), rt)
    enviadas: list[tuple[str, str]] = []

    async def _enviar(chat_id: str, texto: str) -> None:
        enviadas.append((chat_id, texto))

    async def _nada(*_a, **_k) -> None:
        return None

    b._enviar = _enviar  # type: ignore[method-assign]
    b._digitando = _nada  # type: ignore[method-assign]
    b.cfg.pausa_entre_bolhas_s = 0.0
    return b, rt, enviadas


async def test_o_session_id_deriva_do_chat(bot) -> None:
    b, _, _ = bot
    assert b.session_id("12345") == "tg:12345"


async def test_mensagem_vira_turno_e_bolhas(bot) -> None:
    b, rt, enviadas = bot
    await b._processar({"update_id": 1, "message": {"text": "oi", "chat": {"id": 99}}})
    # A pausa entre bolhas é proporcional ao tamanho do texto; espera o bastante.
    await asyncio.sleep(1.5)

    assert rt.chamadas == [("tg:99", "oi")]
    assert [t for _c, t in enviadas] == ["Oi!", "Como é seu nome?"]
    assert b.stats.turnos == 1 and b.stats.bolhas == 2


async def test_update_repetido_nao_roda_dois_turnos(bot) -> None:
    b, rt, _ = bot
    update = {"update_id": 7, "message": {"text": "oi", "chat": {"id": 99}}}
    await b._processar(update)
    await b._processar(update)
    await asyncio.sleep(0.3)
    assert len(rt.chamadas) == 1


async def test_mensagem_sem_texto_e_ignorada(bot) -> None:
    """Foto e áudio ainda não são tratados nesta camada."""
    b, rt, _ = bot
    await b._processar({"update_id": 1, "message": {"chat": {"id": 99}}})
    await asyncio.sleep(0.15)
    assert rt.chamadas == []


async def test_comando_nao_passa_pelo_agente(bot) -> None:
    b, rt, enviadas = bot
    await b._processar({"update_id": 1, "message": {"text": "/estado", "chat": {"id": 99}}})
    assert rt.chamadas == []
    assert "nó:" in enviadas[0][1]


async def test_start_sem_saudacao_deixa_o_agente_abrir(bot) -> None:
    """Assim a primeira mensagem já é a do roteiro, na voz da persona."""
    b, rt, _ = bot
    await b._processar({"update_id": 1, "message": {"text": "/start", "chat": {"id": 99}}})
    await asyncio.sleep(0.3)
    assert rt.chamadas == [("tg:99", "oi")]


async def test_falha_no_turno_avisa_o_lead(bot) -> None:
    """Silêncio é pior que um pedido de repetição."""
    b, _rt, enviadas = bot

    async def quebra(*_a, **_k):
        raise RuntimeError("provedor fora do ar")

    b.runtime.turno = quebra  # type: ignore[method-assign]
    await b._rodar_turno("99", "oi")
    assert "problema" in enviadas[0][1].lower()
    assert b.stats.erros == 1


async def test_o_relogio_nao_cancela_o_proprio_turno() -> None:
    """Regressão: o buffer cancelava a task que estava disparando.

    ``_disparar`` cancelava ``r.tarefa`` — que, quando é o próprio relógio a
    disparar, é a task em execução. O cancelamento efetivava no ``await``
    seguinte: a pausa entre bolhas. Só a primeira bolha saía, em silêncio, e
    toda resposta de duas ou mais mensagens ficava pela metade.
    """
    entregues: list[str] = []

    async def turno_de_duas_bolhas(_chat: str, _texto: str) -> None:
        entregues.append("primeira")
        await asyncio.sleep(0.05)  # onde o cancelamento batia
        entregues.append("segunda")

    buf = BufferDeEntrada(turno_de_duas_bolhas, segundos=0.05)
    await buf.receber("c1", "oi")
    await asyncio.sleep(0.3)

    assert entregues == ["primeira", "segunda"]
