"""Protocol + Rejection dataclass for v4 enforcement rules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from zoi_agno.contracts import Command, CommandKind


@dataclass
class Transform:
    """Explicit instruction for the dispatcher to rewrite a command.

    Replaces the legacy ``Rejection.extra['transform_to']`` convention so the
    rejection/transform separation is type-checked (FIND-027).
    """

    to_kind: CommandKind
    payload_overrides: dict[str, Any]
    confidence: float = 1.0
    rationale: str = ""


@dataclass
class Rejection:
    rule: str
    code: str
    command_kind: CommandKind
    detail: str
    extra: dict[str, Any] | None = None
    transform: Transform | None = None  # FIND-027 — explicit rewrite instruction
    # Routine cutover follow-up 2026-05-26 (sid 2709bea6 audit linha 4017→4019):
    # ``finish_flow`` lives in ``_NEVER_KINDS`` (universal escape hatch
    # alongside ``handoff_human``). Without an override a Rejection emitted
    # by ``FinishFlowGraphRule`` is logged but the command is still accepted.
    # ``force_decision`` lets a specific rule downgrade a "never" command to
    # soft/hard for THAT rejection only, leaving the global universal-
    # acceptance contract intact for other rules.
    force_decision: Literal["soft", "hard"] | None = None
    # H1 (runtime review 2026-06-10) — the Command as-checked (the exact
    # object the rule saw, pre any transform rewrite), attached by
    # DispatcherV4 when a rule returns a Rejection. Rules never set this.
    # Lets the audit event carry the real payload instead of a hardcoded
    # husk (payload={}, confidence=0.0) that made rejected slots/values
    # unrecoverable from the audit.
    command: Command | None = None


@runtime_checkable
class EnforcementRuleV4(Protocol):
    name: str

    async def check(
        self,
        cmd: Command,
        state: dict[str, Any],
        ctx: dict[str, Any],
    ) -> Rejection | None: ...


# UAT imob_sdr 2026-05-19 — ``say_freetalk`` moved from hard to soft. A
# rejected say_freetalk is benign (the reasoner produces free text anyway)
# and must NOT abort the rest of the command batch. Pre-fix: cmd-gen often
# emits [say_freetalk, skip_collect, skip_collect, ...]; say_scope rejects
# say_freetalk → hard_break → 4 skip_collects silently dropped, neither
# accepted nor audited. Now soft → drop the say_freetalk only.
_SOFT_FAIL_KINDS = {"set_slot", "confirm_slot", "signal", "say_freetalk"}
_HARD_FAIL_KINDS = {
    "start_subflow",
    "cancel_subflow",
    "replan",
    "skip_collect",
    "clarify",
    "consult_faq",
}
_NEVER_KINDS = {"handoff_human", "finish_flow"}


def rejection_decision(kind: CommandKind) -> Literal["soft", "hard", "never"]:
    if kind in _SOFT_FAIL_KINDS:
        return "soft"
    if kind in _NEVER_KINDS:
        return "never"
    return "hard"
