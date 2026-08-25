"""Nome de sinal não é texto para o lead.

Os sinais (``escolheu``, ``respondido``, ``quer_humano``) são vocabulário
interno: o extrator os emite como comando e o ``decide`` seguinte os consome.
O lead nunca deveria vê-los.

Vazam porque o ``goal`` do freetalk é entregue ao redator como TAREFA, e o
redator é um gerador de texto: uma linha "emita ``respondido``" é cumprida
escrevendo a palavra. ``composer._goal_para_o_redator`` tira essas linhas na
origem; este guarda é a rede, para o caso do modelo escrever o nome sozinho.

**Só o fragmento pendurado é removido**, e a definição é estreita de propósito:
o nome tem que estar no FIM do texto ou da linha, logo depois de uma frase que
já terminou (``.``, ``!``, ``?`` ou quebra de linha). Isso pega o vazamento
real —

    "Quer seguir com o Duster ou com a Tracker? respondido"

— e não toca em prosa legítima que usa a mesma palavra:

    "posso te avisar quando abrir vaga"

que é o motivo de não bastar procurar o nome do sinal em qualquer posição:
``avisar``, ``gostou`` e ``escolheu`` também são palavras comuns.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


def limpar_nomes_de_sinal(texto: str, sinais: list[str]) -> tuple[str, list[str]]:
    """Devolve ``(texto_limpo, sinais_removidos)``.

    Remoção, e não rejeição: o resto da frase estava certo, e derrubar a bolha
    inteira para o template genérico do roteiro seria pior que tirar uma
    palavra pendurada no fim.
    """
    if not texto or not sinais:
        return texto, []

    removidos: list[str] = []
    limpo = texto
    for sinal in sinais:
        # (frase terminada)(espaços)(`sinal` ou sinal)(pontuação final opcional)(fim de linha/texto)
        padrao = re.compile(
            rf"(?<=[.!?\n])[ \t]*`?{re.escape(sinal)}`?[ \t]*[.!]?(?=\n|$)",
            re.IGNORECASE,
        )
        novo, n = padrao.subn("", limpo)
        if n:
            removidos.append(sinal)
            limpo = novo

    if removidos:
        limpo = re.sub(r"[ \t]+\n", "\n", limpo).strip()
        logger.warning(
            "guards.nome_de_sinal_no_texto removidos=%s — o redator escreveu "
            "vocabulário interno na bolha do lead",
            removidos,
        )
    return limpo, removidos
