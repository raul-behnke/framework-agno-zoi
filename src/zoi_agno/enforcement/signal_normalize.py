"""SignalNameNormalizeRule — coerce a near-miss signal name to the closest declared one.

Runtime backstop for when cmd-gen emits a ``signal`` whose ``name`` is a near-miss
of a declared signal (prod bug 2026-06-14: the LLM emitted ``pedi_corretor`` for the
declared ``pediu_corretor`` — dropped one char). Without this the exact-match check in
``SignalRateLimitRule`` rejects it (soft) and the turn loops, never reaching the
subflow gated behind that signal (e.g. agendamento).

Mirrors ``ConfidenceThresholdRule``: returns a ``Rejection`` carrying a ``Transform``
so the dispatcher rewrites the command and re-checks it against the remaining rules.
Because this rule runs IMMEDIATELY BEFORE ``SignalRateLimitRule``, the rewritten
(now-canonical) name flows into the exact-match gate and passes.

Safety (verified 2026-06-14: no two signals declared in the same node across any
tenant are within edit distance ≤2): coerces only when exactly ONE declared signal
is an unambiguous close match. No-op when the name already matches (so a pure
case/whitespace difference is still normalised to the canonical casing), when no
declared signals exist (non-FreeTalk node), or when no declared name is close
enough — a genuinely bogus name then falls through to the existing reject path.
"""

from __future__ import annotations

import difflib
from typing import Any

from zoi_agno.contracts import Command
from zoi_agno.enforcement.rule import Rejection, Transform

# Minimum SequenceMatcher ratio for a near-miss to be accepted as the same
# signal. 0.85 catches single-char drops (``pedi_corretor`` vs ``pediu_corretor``
# ≈ 0.96) and pure case/accent differences, while rejecting a plausible-but-
# distinct LLM output that shares a long prefix — e.g. ``ajustar_preco`` scores
# exactly 0.80 to ``ajustar_tipo`` and must NOT be coerced (review 2026-06-14).
# Stricter than difflib's 0.6 default on purpose — this rewrites a command.
_MIN_RATIO = 0.85
# Required gap between the best and second-best match. Guards against coercing a
# name that sits roughly equidistant between two declared signals (ambiguous).
_AMBIGUITY_GAP = 0.08
# IEEE-754 slack: `0.8 - 0.08 == 0.7200000000000001`, so a naive `>=` comparison
# fails to flag a gap that is EXACTLY _AMBIGUITY_GAP. Compare with tolerance.
_EPS = 1e-9


def _closest_declared(name: str, declared: list[str]) -> str | None:
    """Return the canonical declared signal ``name`` is an unambiguous near-miss
    of, or ``None``. Case-insensitive; always returns the declared casing."""
    if not declared:
        return None
    nl = name.strip().lower()
    if not nl:
        return None
    lower_map = {d.lower(): d for d in declared}
    if nl in lower_map:
        return lower_map[nl]  # exact (case/whitespace-insensitive)
    scored = sorted(
        ((difflib.SequenceMatcher(None, nl, d.lower()).ratio(), d) for d in declared),
        key=lambda t: t[0],
        reverse=True,
    )
    best_ratio, best = scored[0]
    if best_ratio < _MIN_RATIO:
        return None
    # Ambiguous when the runner-up is within _AMBIGUITY_GAP of the best. The
    # +_EPS makes the boundary inclusive despite float error (a gap of exactly
    # _AMBIGUITY_GAP counts as ambiguous → refuse to guess).
    if len(scored) > 1 and (best_ratio - scored[1][0]) <= _AMBIGUITY_GAP + _EPS:
        return None
    return best


class SignalNameNormalizeRule:
    name = "signal_normalize"

    async def check(
        self,
        cmd: Command,
        state: dict[str, Any],
        ctx: dict[str, Any],
    ) -> Rejection | None:
        if cmd.kind != "signal":
            return None
        declared = list(ctx.get("node_signals", []) or [])
        if not declared:
            return None
        name = cmd.payload.name
        if name in declared:
            return None  # already exact — nothing to coerce
        match = _closest_declared(name, declared)
        if match is None or match == name:
            return None
        return Rejection(
            self.name,
            "signal_name_coerced",
            cmd.kind,
            f"signal {name!r} coerced to declared {match!r}",
            extra={"from": name, "to": match},
            transform=Transform(
                to_kind="signal",
                payload_overrides={"name": match, "value": cmd.payload.value},
                confidence=cmd.confidence,
                rationale="auto_normalize_signal_name",
            ),
        )
