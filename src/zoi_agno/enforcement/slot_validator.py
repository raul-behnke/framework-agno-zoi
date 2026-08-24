"""SlotValidatorRule — semantic validation of ``set_slot`` payload values.

B3/B4 UAT — sid=ce6dfa83 turn 05 showed bot accepting ``"pinguim"`` as
``forma_pagamento`` and turn 06 accepting ``"oni-chan"`` as ``tem_entrada``.
``ConfidenceThresholdRule`` only gates on the LLM-reported confidence; the
LLM was over-confident on garbage input. ``SlotScopeRule`` only verifies
the slot exists in the flow — accepts any value.

This rule reads the current Collect node's ``zoi:validator`` declaration
(stored as ``CollectNode.validator`` string on the AST, surfaced into
``ctx['current_node_def']['validator']`` by the dispatch stage) and
checks the proposed ``cmd.payload.value`` against the expected shape:

==================== ================================================
``validator`` string  semantic check
==================== ================================================
``sim_nao``          normalize_sim_nao(value) returns ``"sim"`` or ``"nao"``
``opcoes:a,b,c``     value (folded) ∈ option list
``text`` / ``texto`` non-empty after strip; rejects pure punctuation
``numero`` / ``valor`` ``float(value)`` coerces (accepts "30", "30k", "30 mil")
(missing / unknown)  no-op (BPMN authors who didn't declare validator
                     get pre-rule behaviour preserved)
==================== ================================================

Rejection carries a ``Transform(to_kind="clarify", ...)`` so the
dispatcher rewrites the broken ``set_slot`` into a ``clarify`` command
with the node's prompt as the question — Lead gets a real re-prompt
instead of state being silently advanced or a generic "não entendi".

Only triggers on ``set_slot``. ``confirm_slot`` already has a
human-confirmation handshake; not gated here.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from zoi_agno.contracts import Command
from zoi_agno.enforcement.rule import Rejection, Transform
from zoi_agno.sim_nao_normalizer import normalize_sim_nao

# Match Brazilian-money shorthand at the head of the string ("30k", "30 mil",
# "30 pila", "30 reais"). Coercer below normalises any of these to the
# integer/float equivalent BEFORE the numeric check.
_NUMERIC_SUFFIX_RE = re.compile(
    r"^\s*([\d.,]+)\s*(k|mil|pila|paus|reais|r\$)?\s*$",
    re.IGNORECASE,
)


def _fold(s: Any) -> str:
    """ASCII-fold + lowercase + strip. ``'Manhã'`` -> ``'manha'``.

    Used by the ``opcoes:*`` validator branch to compare folded user values
    against folded option labels + folded synonyms.
    """
    nfkd = unicodedata.normalize("NFKD", str(s))
    return "".join(c for c in nfkd if not unicodedata.combining(c)).strip().lower()


# C4 wave 2 — canonical option label -> tuple of accepted folded paraphrases.
# Keys are BPMN-declared option labels, folded (lowercase, no accents).
# Synonyms are pre-folded.
#
# NOTE: sim/nao deliberately NOT here — the dedicated ``validator="sim_nao"``
# path uses ``normalize_sim_nao`` which is richer than substring match.
# Synonyms match as whole words (``\b{syn}\b``) against the folded value, so
# short synonyms (e.g. ``"zap"``) do not false-positive on unrelated user
# text (e.g. ``"zapatista"``).
_OPTIONS_SYNONYMS: dict[str, tuple[str, ...]] = {
    "semana": ("semana", "durante a semana", "dia util", "dia de semana", "semanal", "dias uteis"),
    "sabado": ("sabado", "fim de semana", "fds", "weekend", "final de semana"),
    # Dias específicos (2026-07-16) — variantes "-feira" pro caminho opcoes:*
    # (que não faz containment; o _check_slot_enum faz e já pega "sexta às 15").
    "segunda": ("segunda", "segunda feira", "segunda-feira"),
    "terca": ("terca", "terca feira", "terca-feira"),
    "quarta": ("quarta", "quarta feira", "quarta-feira"),
    "quinta": ("quinta", "quinta feira", "quinta-feira"),
    "sexta": ("sexta", "sexta feira", "sexta-feira"),
    "domingo": ("domingo",),
    "manha": ("manha", "pela manha", "matutino", "cedo", "de manha"),
    "tarde": ("tarde", "pela tarde", "fim da tarde", "vespertino", "a tarde"),
    "qualquer": (
        "qualquer",
        "tanto faz",
        "qualquer dia",
        "qualquer hora",
        "indiferente",
        "sem preferencia",
    ),
    "whatsapp": ("whatsapp", "zap", "whats", "aqui mesmo", "por aqui"),
    "ligacao": ("ligacao", "telefone", "ligar", "celular", "por telefone"),
    # Papel de decisão (zoi_sdr QA 2026-07-06): quem se declara da linha de
    # frente sem poder de decisão mapeia direto pra nao_decisor; afirmações
    # de posse/decisão mapeiam pra dono/decisor (o extractor tende a emitir a
    # fala verbatim — "sou eu mesmo, sim" — em vez do token do enum, e o drop
    # repetido travava a abertura em loop, r8).
    "nao_decisor": (
        "vendedor",
        "vendedora",
        "funcionario",
        "funcionaria",
        "atendente",
        "corretor",
        "corretora",
        "nao",
        "nao decido",
        "nao sou eu que decido",
        "quem decide e o dono",
        "quem decide e o chefe",
    ),
    "dono": (
        "dono",
        "dona",
        "proprietario",
        "proprietaria",
        "socio",
        "socia",
        "fundador",
        "fundadora",
        "ceo",
    ),
    "decisor": (
        "decisor",
        "decisora",
        "sim",
        "sou eu",
        "sou eu mesmo",
        "eu mesmo",
        "eu mesma",
        "eu que decido",
        "decido tudo",
        "eu decido",
        "a frente do comercial",
    ),
    "manual": ("manual", "planilha", "caderno", "na mao", "manualmente"),
    "trafego": ("trafego", "trafego pago", "anuncio", "anuncios", "ads", "meta ads", "google ads"),
    "prospeccao": ("prospeccao", "prospeccao ativa", "ativo", "cold call", "lista fria"),
}


def _coerce_numeric(value: Any) -> float | None:
    """Best-effort numeric coercion. Returns None when not coercible."""
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    s = value.strip().replace(",", ".")
    m = _NUMERIC_SUFFIX_RE.match(s)
    if not m:
        # Fall back to direct float — handles "30000", "30000.50"
        try:
            return float(s)
        except ValueError:
            return None
    head, suffix = m.group(1), (m.group(2) or "").lower()
    try:
        n = float(head)
    except ValueError:
        return None
    if suffix in {"k", "mil"}:
        n *= 1000.0
    return n


def _parse_options(validator: str) -> list[str]:
    """Extract option tokens from ``opcoes:a,b,c`` validator string.

    Returns lowercased + stripped tokens. Empty list when format unexpected.
    """
    _, _, rest = validator.partition(":")
    return [t.strip().lower() for t in rest.split(",") if t.strip()]


# Extras de 1ª pessoa pra enums sim/nao que o normalize_sim_nao não cobre
# (confirmações contextuais tipo "sou eu mesmo" à pergunta "você decide?").
# Negações checadas ANTES (contêm as afirmações como substring).
# Ordem importa: padrões ESPECÍFICOS antes dos genéricos. "Não, sou eu mesmo"
# (resposta enfática a "tem mais alguém?") deve resolver pela afirmação de 1ª
# pessoa, não pelo "não" solto (r18: dono foi coagido pra 'nao' e repassado).
_SIM_NAO_ENUM_EXTRA: tuple[tuple[str, str], ...] = (
    (r"\bquem decide e (o|a)\b", "nao"),
    (r"\b(ele|ela) (que )?decide\b", "nao"),
    (r"\bo (dono|chefe|socio) (que )?decide\b", "nao"),
    (r"\b(vendedor|vendedora|funcionario|funcionaria|atendente)\b", "nao"),
    (
        r"\b(sou eu|eu mesmo|eu mesma|eu que decido|eu decido|passa por mim"
        r"|sou o dono|sou a dona|sou dono|sou dona|sou socio|sou socia)\b",
        "sim",
    ),
    (r"\bnao (sou eu que )?decido\b", "nao"),
)
# ^sim/^nao/\bnao\b genéricos REMOVIDOS (r27): o extractor arquiva frases de
# outras perguntas no slot ("Não é tudo que vira venda" → 'nao' → repasse
# indevido). Negações/afirmações LIMPAS resolvem pelo normalize_sim_nao;
# contextuais só pelos padrões específicos acima; o resto dropa (re-pergunta).


class SlotValidatorRule:
    name = "slot_validator"

    async def check(
        self,
        cmd: Command,
        state: dict[str, Any],
        ctx: dict[str, Any],
    ) -> Rejection | None:
        if cmd.kind != "set_slot":
            return None

        # ---- enum por DECLARAÇÃO de slot (routine DSL) — vale em QUALQUER
        # node (freetalk incluso). QA zion 2026-07-06: extractor gravou
        # "vendedora" / "Sim, sou eu mesmo..." em cargo_decisor (enum
        # [dono, decisor, nao_decisor]) e quebrou todo decide downstream. ----
        enum_rejection = self._check_slot_enum(cmd, ctx)
        if enum_rejection is not None:
            return enum_rejection

        node_def = ctx.get("current_node_def") or {}
        if node_def.get("kind") != "collect":
            # Validator only meaningful at Collect nodes.
            return None

        # Only validate when the proposed slot matches the current Collect's
        # field — set_slot on an out-of-current-node slot (e.g. proactive
        # set from an earlier turn) is the SlotScopeRule's job, not ours.
        slot = cmd.payload.slot
        node_field = node_def.get("field")
        if node_field and slot != node_field:
            return None

        validator = (node_def.get("validator") or "").strip().lower()
        if not validator:
            return None  # author opted out of validation

        value = cmd.payload.value
        prompt = (node_def.get("prompt") or "").strip()

        # ---- sim_nao ----
        if validator == "sim_nao":
            normalized = normalize_sim_nao(str(value)) if value is not None else None
            if normalized in {"sim", "nao"}:
                return None
            return self._reject(cmd, "sim_nao_invalid", value, prompt, validator)

        # ---- opcoes:a,b,c ----
        if validator.startswith("opcoes:"):
            options = _parse_options(validator)
            if not options:
                # Malformed validator — author error; do not silently accept.
                return self._reject(cmd, "options_validator_malformed", value, prompt, validator)
            folded_value = _fold(value) if value is not None else ""
            if not folded_value:
                return self._reject(cmd, "value_not_in_options", value, prompt, validator)
            for opt in options:
                opt_folded = _fold(opt)
                # 1) Exact folded match (current behaviour, preserved).
                if folded_value == opt_folded:
                    return None
                # 2) Whole-word synonym match. \b uses ASCII word boundary,
                # which is correct because folded_value is ASCII-only
                # (NFKD-stripped above).
                for syn in _OPTIONS_SYNONYMS.get(opt_folded, ()):
                    if re.search(rf"\b{re.escape(syn)}\b", folded_value):
                        return None
            return self._reject(cmd, "value_not_in_options", value, prompt, validator)

        # ---- text / texto ----
        if validator in {"text", "texto"}:
            if value is None:
                return self._reject(cmd, "text_empty", value, prompt, validator)
            s = str(value).strip()
            # Reject pure punctuation or single-char tokens — too short to be
            # a real answer. ``"30 pila"`` etc. are >=2 chars and contain
            # alphanumerics so they pass — numeric content is fine in a text
            # slot per BPMN intent (lucas_auto_v2.bpmn uses validator="text"
            # on valor_entrada, which legitimately stores "30000").
            if len(s) < 2:
                return self._reject(cmd, "text_too_short", value, prompt, validator)
            if not re.search(r"[\w\d]", s, flags=re.UNICODE):
                return self._reject(cmd, "text_punctuation_only", value, prompt, validator)
            return None

        # ---- numero / valor ----
        if validator in {"numero", "valor"}:
            n = _coerce_numeric(value)
            if n is None:
                return self._reject(cmd, "value_not_numeric", value, prompt, validator)
            return None

        # Unknown validator string — be conservative and no-op so authors
        # adding new validator types do not silently break dispatch.
        return None

    def _check_slot_enum(self, cmd: Command, ctx: dict[str, Any]) -> Rejection | None:
        """Valor de ``set_slot`` num slot declarado ``type: enum`` precisa
        resolver pra um dos ``values`` declarados.

        Resolução, na ordem: match exato (folded) → sinônimo whole-word
        (_OPTIONS_SYNONYMS) → containment whole-word de exatamente UM valor
        do enum ("tráfego pago" → "trafego"). Resolvido ≠ literal → Transform
        reescreve o set_slot com o valor canônico (coerção). Irresolvível →
        Transform pra clarify (re-pergunta), igual aos validators de Collect.
        """
        business = ctx.get("business") or {}
        if isinstance(business, dict) and business.get("enum_slot_validation", True) is False:
            return None  # kill-switch por tenant — false explícito desliga (default-on 2026-07-07)
        enums = ctx.get("slot_enums") or {}
        declared = enums.get(cmd.payload.slot)
        if not declared:
            return None
        value = cmd.payload.value
        folded_value = _fold(value) if value is not None else ""
        if not folded_value:
            return self._reject(cmd, "enum_value_empty", value, "", f"enum:{cmd.payload.slot}")

        # Enum sim/nao (ex.: decide_confirmado do zoi_sdr): usa o normalizador
        # rico de afirmações PT-BR + extras de 1ª pessoa ("sou eu mesmo").
        if {_fold(v) for v in declared} == {"sim", "nao"}:
            normalized = normalize_sim_nao(str(value))
            if normalized is None:
                for pat, target in _SIM_NAO_ENUM_EXTRA:
                    if re.search(pat, folded_value):
                        normalized = target
                        break
            if normalized in {"sim", "nao"}:
                if folded_value == normalized:
                    return None
                return Rejection(
                    rule=self.name,
                    code="enum_value_coerced",
                    command_kind=cmd.kind,
                    detail=f"value={value!r} coerced to {normalized!r} (sim/nao enum)",
                    extra={"slot": cmd.payload.slot, "value": str(value)[:120]},
                    transform=Transform(
                        to_kind="set_slot",
                        payload_overrides={"slot": cmd.payload.slot, "value": normalized},
                        confidence=cmd.confidence,
                        rationale=f"slot_validator:enum_coerced:{normalized}",
                    ),
                )
            return Rejection(
                rule=self.name,
                code="enum_value_unresolvable",
                command_kind=cmd.kind,
                detail=f"value={value!r} not resolvable to sim/nao",
                extra={"slot": cmd.payload.slot, "value": str(value)[:120]},
            )

        # Melhor match por canônico = MAIOR token casado (sinônimo, token do
        # enum ou variante com espaço). Desempate entre canônicos pelo
        # comprimento: "sim, sou o dono" → dono(4) > sim(3); "quem decide é o
        # dono" → sinônimo longo de nao_decisor(19) > dono(4). Empate real →
        # ambíguo → drop.
        best_len: dict[str, int] = {}
        for canonical in declared:
            canon_folded = _fold(canonical)
            spaced = canon_folded.replace("_", " ")  # "nao_decisor" dito "não decisor"
            if folded_value == canon_folded:
                return None  # já canônico
            tokens = (canon_folded, spaced, *_OPTIONS_SYNONYMS.get(canon_folded, ()))
            for tok in tokens:
                if re.search(rf"\b{re.escape(tok)}\b", folded_value):
                    best_len[canonical] = max(best_len.get(canonical, 0), len(tok))
        hits: list[str] = []
        if best_len:
            top = max(best_len.values())
            hits = [c for c, n in best_len.items() if n == top]
        if len(hits) == 1:
            return Rejection(
                rule=self.name,
                code="enum_value_coerced",
                command_kind=cmd.kind,
                detail=f"value={value!r} coerced to {hits[0]!r} (enum {cmd.payload.slot})",
                extra={"slot": cmd.payload.slot, "value": str(value)[:120], "coerced": hits[0]},
                transform=Transform(
                    to_kind="set_slot",
                    payload_overrides={"slot": cmd.payload.slot, "value": hits[0]},
                    confidence=cmd.confidence,
                    rationale=f"slot_validator:enum_coerced:{hits[0]}",
                ),
            )
        # Irresolvível → DROP silencioso (soft-fail, sem transform): o slot
        # fica vazio e o extractor re-tenta no próximo turno com a conversa
        # seguindo natural. Clarify aqui era pior (live r6): "Não entendi"
        # genérico após resposta clara confundia o lead E o extractor.
        return Rejection(
            rule=self.name,
            code="enum_value_unresolvable",
            command_kind=cmd.kind,
            detail=f"value={value!r} not resolvable to enum {declared!r}",
            extra={"slot": cmd.payload.slot, "value": str(value)[:120]},
        )

    def _reject(
        self,
        cmd: Command,
        code: str,
        value: Any,
        prompt: str,
        validator: str,
    ) -> Rejection:
        """Build a Rejection that rewrites the set_slot into a clarify."""
        fallback_question = "Não entendi. Pode me explicar de outro jeito?"
        question = prompt or fallback_question
        return Rejection(
            rule=self.name,
            code=code,
            command_kind=cmd.kind,
            detail=f"value={value!r} fails validator={validator!r}",
            extra={
                "slot": cmd.payload.slot,
                "value": str(value)[:120],
                "validator": validator,
            },
            transform=Transform(
                to_kind="clarify",
                payload_overrides={
                    "question": question,
                    "options": _parse_options(validator) if validator.startswith("opcoes:") else [],
                },
                confidence=1.0,
                rationale=f"slot_validator:{code}",
            ),
        )
