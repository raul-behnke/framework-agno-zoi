"""InterrogativeUserMsgRule — reject ``set_slot`` when the user's last
message is a question rather than an answer.

B2 UAT — sid=ce6dfa83 turn 04. Lead asked "tem algum outro?" while
the bot was at C_confirma (validator=sim_nao). Cmd-gen LLM
hallucinated ``set_slot(confirma_escolha, "sim", confidence=0.X)`` —
extracting a positive answer from a question. ``SlotValidatorRule``
cannot catch this because "sim" IS a valid sim_nao value; the bug
is upstream in user-intent classification, not value shape.

This rule fires BEFORE ``SlotValidatorRule`` in the rule chain. It
reads ``state['_last_user_msg']`` (populated by TurnManager stage 1)
and rejects ``set_slot`` for sim_nao / opcoes Collect nodes when:

  1. The user message ends with ``"?"`` after stripping whitespace
     + trailing emoji, OR
  2. The user message starts with a PT-BR interrogative pattern
     (qual, quais, como, quando, onde, por que, quem, quanto, etc).

Bypass: if the user message STARTS with a token from
``sim_nao_normalizer._AFFIRMATIONS`` or ``_NEGATIONS`` (e.g. "sim?",
"não tem outro?"), pass through — the lead is tagging an answer
with a question mark, the cmd-gen extraction is fine.

Only triggers on ``set_slot`` to current Collect node's field
(matches SlotValidatorRule scope). ``confirm_slot`` already has a
human-in-the-loop handshake; not gated.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from zoi_agno.contracts import Command
from zoi_agno.enforcement.rule import Rejection, Transform
from zoi_agno.sim_nao_normalizer import (
    _AFFIRMATIONS,
    _NEGATIONS,
)

# PT-BR interrogative openers (folded + lowercased). Match when the user
# message starts with one of these (word-boundary), independent of trailing
# question mark. The "?" alone is also a sufficient signal — see below.
_INTERROGATIVE_OPENERS: frozenset[str] = frozenset(
    {
        "qual",
        "quais",
        "como",
        "quando",
        "onde",
        "por que",
        "porque",
        "pq",
        "pq?",
        "quem",
        "que",
        "quanto",
        "quantos",
        "quantas",
        "tem algum",
        "tem alguma",
        "tem outro",
        "tem outra",
        "tem mais",
        "tem como",
        "voce tem",
        "vc tem",
        "voces tem",
        "vcs tem",
        "da pra",
        "e possivel",
        "tem possibilidade",
    }
)


def _fold(text: str) -> str:
    """Lowercase + accent-fold + collapse whitespace + strip trailing punct
    except ``?``. We KEEP the ``?`` because its presence is a signal we
    look for separately.
    """
    norm = unicodedata.normalize("NFD", text)
    no_accents = "".join(c for c in norm if unicodedata.category(c) != "Mn")
    lowered = no_accents.lower().strip()
    # Collapse multi-space, strip trailing punctuation EXCEPT '?'.
    lowered = re.sub(r"\s+", " ", lowered)
    lowered = re.sub(r"[.,!;:]+$", "", lowered).strip()
    return lowered


def _starts_with_answer_token(folded_msg: str) -> bool:
    """True iff the message starts with an affirmation or negation token
    from the sim_nao normalizer — that is the legitimate ``"sim?"`` /
    ``"não tem outro?"`` tagging path.
    """
    for token in _AFFIRMATIONS | _NEGATIONS:
        if folded_msg == token:
            return True
        # Word-bound prefix match — "sim" matches "sim?", "sim mesmo",
        # "sim ai" but not "simples" or "simpatico".
        if (
            folded_msg.startswith(token + " ")
            or folded_msg.startswith(token + "?")
            or folded_msg.startswith(token + ",")
        ):
            return True
    return False


# PT-BR terminal confirmation tags. Affixed to an affirmation, they read as
# polar question marks ("fica bom na quarta as 9:00 pode ser?", "vou pegar
# esse né?"), not as open questions. UAT imob_sdr 2026-05-19 turn 14:
# lead said "fica bom na quarta as 9:00 pode ser?" → cmd-gen emitted
# set_slot(dia_agendamento, "quarta"), rule rejected via Marker 1. We strip
# the tag here so the affirmation body alone is evaluated.
_TERMINAL_TAGS: tuple[str, ...] = (
    "pode ser",
    "tudo bem",
    "ta bom",
    "ta certo",
    "ta tranquilo",
    "ne",  # already folded from "né"
    "ta",  # already folded from "tá"
    "certo",
    "beleza",
    "ok",
    "concorda",
)


def _strip_terminal_tag(folded_msg: str) -> str:
    """If the message ends with ``"?"`` and the body terminates in a known
    confirmation tag (e.g. ``"pode ser"``, ``"né"``, ``"ta"``), return the
    body with the tag removed. Otherwise return the input unchanged.

    Returns the original input when the message is exactly the tag (no
    affirmation body) — that case is a genuine polar question.
    """
    if not folded_msg.endswith("?"):
        return folded_msg
    body = folded_msg[:-1].rstrip(" ,.;")
    # Sort by length descending so "ta bom" matches before "ta".
    for tag in sorted(_TERMINAL_TAGS, key=len, reverse=True):
        if body == tag:
            return folded_msg
        if body.endswith(" " + tag) or body.endswith("," + tag):
            stripped = body[: -len(tag)].rstrip(" ,.;")
            if stripped:
                return stripped
    return folded_msg


def _abre_interrogativa(folded: str) -> bool:
    """True quando o trecho COMEÇA com um padrão interrogativo."""
    for opener in _INTERROGATIVE_OPENERS:
        if folded == opener or folded.startswith(opener + " ") or folded.startswith(opener + "?"):
            return True
    return False


def _tem_afirmacao_antes_da_pergunta(user_msg: str) -> bool:
    """True quando a mensagem AFIRMA alguma coisa e só depois pergunta.

    A regra original olha a mensagem inteira: se termina em "?", nada dela
    vira slot. Isso mata o padrão mais comum de lead real, que responde e
    pergunta na mesma tacada:

        "Tenho uma Ecosport 2012
         Aceitam?"

    O lead RESPONDEU que tem carro na troca. A pergunta é sobre outra coisa.
    Sem esta porta, o `set_slot` é descartado, o nó não avança e o agente
    repete a pergunta que acabou de ser respondida — foi exatamente o que
    aconteceu na sessão de 2026-08-25.

    Conservador de propósito: exige que a pergunta esteja no ÚLTIMO trecho e
    que algum trecho ANTERIOR seja declarativo. "tem algum outro?" continua
    barrado (um trecho só), e "quais modelos voces tem, no geral?" também
    (o primeiro trecho já abre interrogativo).
    """
    partes = [_fold(p) for p in re.split(r"[\n,;.!]+", user_msg)]
    partes = [p for p in partes if p]
    if len(partes) < 2:
        return False
    ultima = partes[-1]
    if not (ultima.endswith("?") or _abre_interrogativa(ultima)):
        return False
    return any(not p.endswith("?") and not _abre_interrogativa(p) for p in partes[:-1])


def _is_interrogative(user_msg: str) -> bool:
    """Return True when the message appears to be a question."""
    if not user_msg:
        return False
    folded = _fold(user_msg)
    if not folded:
        return False
    if _starts_with_answer_token(folded):
        return False
    # Marker 1 — explicit question mark at end of message.
    if folded.endswith("?"):
        stripped = _strip_terminal_tag(folded)
        if stripped != folded and not stripped.endswith("?"):
            # Affirmation + confirmation tag ("fica bom pode ser?"). Re-check
            # whether the stripped body itself matches an interrogative
            # opener — that would be a genuine question with a trailing tag
            # ("qual valor pode ser?") and we keep interrogative=True.
            for opener in _INTERROGATIVE_OPENERS:
                if stripped == opener or stripped.startswith(opener + " "):
                    return True
            return False
        return True
    # Marker 2 — opens with an interrogative pattern.
    for opener in _INTERROGATIVE_OPENERS:
        if folded == opener:
            return True
        if folded.startswith(opener + " ") or folded.startswith(opener + "?"):
            return True
    return False


class InterrogativeUserMsgRule:
    name = "interrogative_user_msg"

    async def check(
        self,
        cmd: Command,
        state: dict[str, Any],
        ctx: dict[str, Any],
    ) -> Rejection | None:
        if cmd.kind != "set_slot":
            return None

        node_def = ctx.get("current_node_def") or {}
        if node_def.get("kind") != "collect":
            return None

        validator = (node_def.get("validator") or "").strip().lower()
        # Gate sim_nao + opcoes + text/texto. The first two are where the
        # cmd-gen LLM most commonly hallucinates an answer from a question
        # (small fixed answer space → high prior to pick one). text/texto
        # was added 2026-05-18 after UAT session e3ee41d9 turn 3: lead
        # asked "quais modelos vocês tem?" at C_veiculo (validator=text)
        # and cmd-gen still extracted set_slot(veiculo_interesse="SUV").
        # When the message is interrogative AND opens with a plural-
        # exploration pattern, treat it as a question, not an answer.
        if not (
            validator == "sim_nao"
            or validator.startswith("opcoes:")
            or validator in {"text", "texto"}
        ):
            return None

        slot = cmd.payload.slot
        node_field = node_def.get("field")
        if node_field and slot != node_field:
            return None

        user_msg = state.get("_last_user_msg") or ""
        if not isinstance(user_msg, str):
            return None
        if not _is_interrogative(user_msg):
            return None
        # Respondeu E perguntou na mesma mensagem: a resposta é legítima.
        if _tem_afirmacao_antes_da_pergunta(user_msg):
            return None

        prompt = (node_def.get("prompt") or "").strip()
        fallback_question = "Você quer seguir com essa opção ou prefere ver outras?"
        question = prompt or fallback_question
        return Rejection(
            rule=self.name,
            code="interrogative_user_msg",
            command_kind=cmd.kind,
            detail=(
                f"user_msg={user_msg[:80]!r} looks interrogative; cmd-gen likely "
                f"hallucinated value={cmd.payload.value!r} for slot={slot!r}"
            ),
            extra={
                "slot": slot,
                "value": str(cmd.payload.value)[:120],
                "validator": validator,
                "user_msg_preview": user_msg[:120],
            },
            transform=Transform(
                to_kind="clarify",
                payload_overrides={
                    "question": question,
                    "options": [],
                },
                confidence=1.0,
                rationale=f"interrogative_user_msg:{validator}",
            ),
        )
