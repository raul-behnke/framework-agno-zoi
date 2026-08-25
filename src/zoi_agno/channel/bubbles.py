"""Uma resposta vira bolhas, como gente digitando.

Ninguém manda um parágrafo de quatro linhas no WhatsApp. Manda uma ideia,
depois outra. O redator já escreve em linhas curtas; aqui elas viram mensagens
separadas, com pausa entre uma e outra.

Duas coisas diferentes acontecem neste módulo:

**Corte conversacional** — quebra por linha em branco, ou por linha simples
quando o texto é curto. É estética de conversa.

**Corte duro do Telegram** — 4096 *unidades UTF-16*, não caracteres. Emoji
fora do plano básico contam 2. Portado do v4, onde a contagem ingênua por
caractere truncava mensagem com emoji.
"""

from __future__ import annotations

import re

MAX_UTF16 = 4096
"""Limite de ``sendMessage`` do Telegram, em unidades UTF-16."""

MAX_BOLHAS = 4
"""Acima disso vira metralhadora. O excedente é reagrupado na última."""

_MIN_PARA_QUEBRAR = 12
"""Abaixo disto é reconhecimento ("Show!", "Boa!"), não frase.

O limiar separa tique de conversa: "Show!" sozinho numa mensagem parece
robô com soluço; "Perfeito, Ana." é uma frase e merece a própria bolha.
Calibrado por observação, não por teoria — mexa junto com os testes.
"""


def utf16_len(s: str) -> int:
    """Comprimento em unidades UTF-16, que é como o Telegram conta."""
    return len(s.encode("utf-16-le")) // 2


def cortar_no_limite(texto: str) -> list[str]:
    """Divide no limite duro do Telegram, preservando pares substitutos.

    Caminhar por caractere Python contando o custo em UTF-16 é o que evita
    partir um emoji no meio.
    """
    if utf16_len(texto) <= MAX_UTF16:
        return [texto]
    partes: list[str] = []
    atual: list[str] = []
    custo = 0
    for ch in texto:
        c = utf16_len(ch)
        if custo + c > MAX_UTF16:
            partes.append("".join(atual))
            atual, custo = [ch], c
        else:
            atual.append(ch)
            custo += c
    if atual:
        partes.append("".join(atual))
    return partes


def em_bolhas(texto: str, *, max_bolhas: int = MAX_BOLHAS) -> list[str]:
    """Quebra a resposta em mensagens separadas.

    Prefere linha em branco (parágrafo). Não havendo, usa linha simples —
    é assim que o redator separa ideias. Linhas muito curtas ficam junto da
    anterior para não virar fragmento solto.
    """
    limpo = (texto or "").strip()
    if not limpo:
        return []

    # Parágrafo é separação INTENCIONAL: linha em branco é o autor dizendo
    # "isto é outra mensagem". Quebra de linha simples pode ser só quebra de
    # linha, e aí um fragmento curto vira tique em vez de conversa.
    blocos = [b.strip() for b in re.split(r"\n\s*\n", limpo) if b.strip()]
    por_paragrafo = len(blocos) > 1
    if not por_paragrafo:
        blocos = [linha.strip() for linha in limpo.split("\n") if linha.strip()]

    juntadas: list[str] = []
    for b in blocos:
        curto_demais = not por_paragrafo and len(b) < _MIN_PARA_QUEBRAR
        if juntadas and curto_demais:
            juntadas[-1] = f"{juntadas[-1]}\n{b}"
        else:
            juntadas.append(b)

    # Abertura curta ("Show!", "Perfeito.") gruda na bolha seguinte: sozinha
    # ela vira tique, não conversa. Sem a fusão para trás isso escapava,
    # porque não há bolha anterior a que se juntar.
    if not por_paragrafo and len(juntadas) > 1 and len(juntadas[0]) < _MIN_PARA_QUEBRAR:
        juntadas = [f"{juntadas[0]}\n{juntadas[1]}", *juntadas[2:]]

    # Excedente vai todo para a última — melhor uma bolha longa que oito.
    if len(juntadas) > max_bolhas:
        cabeca = juntadas[: max_bolhas - 1]
        cauda = "\n".join(juntadas[max_bolhas - 1 :])
        juntadas = [*cabeca, cauda]

    return [p for b in juntadas for p in cortar_no_limite(b)]


def pausa_de_digitacao(
    texto: str, *, base: float = 0.6, por_caractere: float = 0.012, teto: float = 3.5
) -> float:
    """Quanto esperar antes de mandar esta bolha.

    Proporcional ao tamanho, com teto. Resposta instantânea entrega o robô
    tanto quanto o texto errado; pausa longa demais parece travamento.
    """
    return min(teto, base + len(texto) * por_caractere)
