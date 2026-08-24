"""Normalizer determinístico para fields `validator="sim_nao"`.

Garante que afirmações naturais em PT-BR ("é esse mesmo", "tudo certo",
"exato", "isso", "claro", etc.) e negações ("não", "de jeito nenhum",
"nem", "nao mesmo") sejam normalizadas em "sim"/"nao" — independente
da estocasticidade do extractor LLM.

Bug histórico (Phase 3 do plano 2026-05-02-subflow-fix): lead dizia
"tudo certo então, SUV cinza..." e o extractor LLM ora retornava o
texto verbatim, ora classificava como `topic_change`, deixando o
Collect `confirmou_veiculo` preso. Esse normalizer roda DEPOIS do
extractor e converte qualquer afirmação reconhecível em "sim".

Inputs ambíguos ("talvez", "hmm", "talvez sim") retornam None para
preservar o caminho de re-prompt do socializer.
"""

from __future__ import annotations

import re
import unicodedata

# Afirmações canônicas reconhecidas como "sim" (após accent-fold + lower).
_AFFIRMATIONS: frozenset[str] = frozenset(
    {
        "sim",
        "yes",
        "yep",
        "yeah",
        "ok",
        "okay",
        "blz",
        "beleza",
        "e",
        "eh",
        "e isso",
        "e esse",
        "e esse mesmo",
        "e isso mesmo",
        "e isso ai",
        "esse mesmo",
        "essa mesma",
        "esse ai",
        "essa ai",
        "isso",
        "isso mesmo",
        "isso ai",
        "tudo certo",
        "tudo bem",
        "exato",
        "exatamente",
        "perfeito",
        "claro",
        "claro que sim",
        "com certeza",
        "certinho",
        "certo",
        "correto",
        "afirmativo",
        "positivo",
        "pode ser",
        "serve",
        "ta bom",
        "ta certo",
        "tah bom",
        "concordo",
        "fechado",
        "feito",
        "bora",
        "bingo",
        "uhum",
        "aham",
        # Afirmações naturais observadas no probe live diag_distraido
        # (2026-05-02 T6/T7) — extractor LLM ocasionalmente devolve essas
        # frases verbatim em vez de "sim", deixando o Collect sim_nao preso.
        "parece bom",
        "ah parece bom",
        "parece otimo",
        "parece legal",
        "gostei",
        "curti",
        "show",
        "massa",
        "tranquilo",
        "demais",
        "vamos",
        "topo",
    }
)

# Negações canônicas reconhecidas como "nao".
_NEGATIONS: frozenset[str] = frozenset(
    {
        "nao",
        "no",
        "nope",
        "nah",
        "nem",
        "nem pensar",
        "nao mesmo",
        "nao e",
        "nao e esse",
        "nao e essa",
        "nao serve",
        "nao gostei",
        "nao quero",
        "negativo",
        "de jeito nenhum",
        "de jeito algum",
        "nunca",
        "jamais",
        "errado",
        "incorreto",
        "outro",
        "outra",
    }
)

# Emojis canônicos de afirmação (match exato pré-fold; emojis sobrevivem
# accent-fold mas pontuação stripping pode alterar). Comparados antes do
# fold pra evitar quebra com ZWJ sequences.
_AFFIRMATIONS_EMOJI: frozenset[str] = frozenset(
    {
        "👍",
        "✅",
        "🆗",
        "👌",
        "💯",
        "🙆",
        "🙆‍♂️",
        "🙆‍♀️",
    }
)

_NEGATIONS_EMOJI: frozenset[str] = frozenset(
    {
        "❌",
        "👎",
        "🚫",
        "🙅",
        "🙅‍♂️",
        "🙅‍♀️",
        "⛔",
    }
)

# Sinais de incerteza — quando presentes, NÃO normaliza pra preservar
# o caminho de ambiguidade/re-prompt do socializer.
_AMBIGUOUS_MARKERS: frozenset[str] = frozenset(
    {
        "talvez",
        "talvez sim",
        "talvez nao",
        "acho que sim",
        "acho que nao",
        "hmm",
        "hum",
        "sei la",
        "nao sei",
        "depende",
        "quem sabe",
    }
)


def _fold(text: str) -> str:
    """Remove acentos + lowercase + collapse whitespace + strip pontuação leve."""
    norm = unicodedata.normalize("NFD", text)
    no_accents = "".join(c for c in norm if unicodedata.category(c) != "Mn")
    lowered = no_accents.lower().strip()
    # Remove pontuação periférica (.,!?;:) — preserva espaços internos.
    lowered = re.sub(r"[.,!?;:]+", " ", lowered)
    lowered = re.sub(r"\s+", " ", lowered).strip()
    return lowered


def normalize_sim_nao(value: str) -> str | None:
    """Normaliza um texto livre em "sim", "nao", ou None (ambíguo/desconhecido).

    Estratégia:
      1. Accent-fold + lowercase + strip.
      2. Se o texto contém marcador de ambiguidade explícito ("talvez",
         "acho que sim", "nao sei") → retorna None.
      3. Match exato no set de afirmações → "sim".
      4. Match exato no set de negações → "nao".
      5. Se começa com afirmação/negação seguida de espaço/pontuação,
         normaliza (ex: "sim, pode ser" → "sim"; "tudo certo entao, suv..."
         → "sim").
      6. Default: None (não normaliza, deixa o caller tratar).
    """
    if not isinstance(value, str):
        return None

    # (1.5) Emoji-only short-circuit — match exato pré-fold pra não perder
    # ZWJ sequences ou variation selectors. Cobre `"👍"`, `"❌"`, `" 👍 "`,
    # `"👍."`. Strip pontuação periférica + whitespace mas preserva emoji
    # multi-codepoint intacto.
    stripped = re.sub(r"[\s.,!?;:]+", "", value).strip()
    if stripped in _AFFIRMATIONS_EMOJI:
        return "sim"
    if stripped in _NEGATIONS_EMOJI:
        return "nao"

    folded = _fold(value)
    if not folded:
        return None

    # (2) Ambiguidade explícita bloqueia normalização.
    for marker in _AMBIGUOUS_MARKERS:
        if marker == folded or folded.startswith(marker + " ") or f" {marker} " in folded:
            return None

    # (3) Match exato afirmação.
    if folded in _AFFIRMATIONS:
        return "sim"

    # (4) Match exato negação.
    if folded in _NEGATIONS:
        return "nao"

    # (5) Match prefixo — pega "tudo certo então, SUV cinza..." → "sim".
    # Ordena do mais longo pro mais curto pra evitar match prematuro
    # (ex: "e" matchando antes de "e esse mesmo").
    for phrase in sorted(_AFFIRMATIONS, key=len, reverse=True):
        if folded == phrase or folded.startswith(phrase + " ") or folded.startswith(phrase + ","):
            return "sim"
    for phrase in sorted(_NEGATIONS, key=len, reverse=True):
        if folded == phrase or folded.startswith(phrase + " ") or folded.startswith(phrase + ","):
            return "nao"

    return None
