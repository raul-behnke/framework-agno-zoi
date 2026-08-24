"""DispatcherV4 — applies rules to a batch of commands.

- Soft fail: drop command, continue batch.
- Hard fail: stop processing remaining commands.
- Never: log rejection but accept command (escape hatch).
- Confidence transform: rewrite set_slot→confirm_slot via Rejection.extra.
"""

from __future__ import annotations

from typing import Any, Literal

from zoi_agno._rejection_helpers import append_rejection
from zoi_agno.contracts import Command
from zoi_agno.enforcement.rule import (
    EnforcementRuleV4,
    Rejection,
    rejection_decision,
)


class DispatcherV4:
    def __init__(self, rules: list[EnforcementRuleV4]) -> None:
        self.rules = list(rules)

    async def dispatch(
        self,
        commands: list[Command],
        state: dict[str, Any],
        ctx: dict[str, Any],
    ) -> tuple[list[Command], list[Rejection]]:
        accepted: list[Command] = []
        rejections: list[Rejection] = []
        hard_break = False
        # UAT imob_sdr 2026-05-19 — when hard_break fires mid-batch, the
        # tail commands were silently dropped (no accept, no reject audit).
        # We now track them so the turn_manager can emit a transparency
        # audit event and they are not invisible to operators.
        dropped_after_hardbreak: list[str] = []

        for cmd in commands:
            # G1 fix (runtime review 2026-06-10) — never-policy commands
            # (handoff_human/finish_flow) must survive a mid-batch hard_break:
            # the universal-escape-hatch contract (RC2) is kind-based, not
            # position-based. They still run through the rules (so per-rejection
            # force_decision, e.g. FinishFlowGraphRule, keeps working).
            if hard_break and rejection_decision(cmd.kind) != "never":
                dropped_after_hardbreak.append(cmd.kind)
                continue
            current = cmd
            cmd_rejection: Rejection | None = None

            for rule in self.rules:
                r = await rule.check(current, state, ctx)
                if r is None:
                    continue
                # H1 — attach the command under check so the turn_manager
                # audit loop logs the real payload (covers transform
                # rejections with the PRE-transform command).
                if r.command is None:
                    r.command = current
                rejections.append(r)
                # FIND-027 — explicit Transform field replaces
                # Rejection.extra['transform_to'] abuse.
                if r.transform is not None:
                    current = Command.model_validate(
                        {
                            "kind": r.transform.to_kind,
                            "payload": dict(r.transform.payload_overrides),
                            "confidence": r.transform.confidence,
                            "rationale": r.transform.rationale,
                        }
                    )
                    continue
                cmd_rejection = r
                break

            # Per-rejection override (force_decision) bypasses the kind-based
            # ``_NEVER_KINDS`` / ``_SOFT_FAIL_KINDS`` policy for THIS rejection
            # only. Required by FinishFlowGraphRule (2026-05-26) so a graph-
            # position rejection of ``finish_flow`` actually drops the cmd
            # while leaving the universal escape-hatch contract for other
            # rejections intact.
            decision: Literal["soft", "hard", "never"]
            if cmd_rejection is not None and cmd_rejection.force_decision is not None:
                decision = cmd_rejection.force_decision
            else:
                decision = rejection_decision(current.kind)
            if cmd_rejection is None or decision == "never":
                accepted.append(current)
                # FIND-032 — track per-turn signal count in ctx so the
                # SignalRateLimitRule "max 1 signal per turn" gate fires
                # on the second signal in the same batch. The counter was
                # always 0 before this wiring (turn_manager seeds 0 + no
                # caller incremented).
                if current.kind == "signal":
                    ctx["signals_emitted_this_turn"] = (
                        int(ctx.get("signals_emitted_this_turn", 0)) + 1
                    )
                continue
            if decision == "soft":
                continue
            hard_break = True

        if dropped_after_hardbreak:
            state["_dropped_after_hardbreak"] = list(dropped_after_hardbreak)

        # FIND-026 — unified shape via helper.
        for r in rejections:
            append_rejection(
                state,
                rule=r.rule,
                code=r.code,
                command_kind=r.command_kind,
                detail=r.detail,
                extra=r.extra,
            )
        return accepted, rejections
