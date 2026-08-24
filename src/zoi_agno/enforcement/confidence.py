"""ConfidenceThresholdRule — low-confidence set_slot transformed to confirm_slot.

Rule emits a Rejection with an explicit ``transform`` field (FIND-027) so the
dispatcher can rewrite the command instead of silently dropping it. This
realises the auto-confirm flow described in spec §7.
"""

from __future__ import annotations

from typing import Any

from zoi_agno.contracts import Command
from zoi_agno.enforcement.rule import Rejection, Transform


class ConfidenceThresholdRule:
    name = "confidence"

    def __init__(self, threshold: float = 0.7) -> None:
        self.threshold = float(threshold)

    async def check(
        self,
        cmd: Command,
        state: dict[str, Any],
        ctx: dict[str, Any],
    ) -> Rejection | None:
        if cmd.kind != "set_slot":
            return None
        if cmd.confidence >= self.threshold:
            return None
        # Decisor-loop fix (2026-06-06) — if this slot already has a pending
        # confirmation, the user has already been asked once. Re-transforming
        # a fresh low-confidence set_slot back into confirm_slot would loop
        # forever (confirm only writes pending_confirmations; the fold to
        # collected lives in the set_slot branch). Let set_slot through so
        # TurnManager folds pending→collected and the loop ends.
        pending = state.get("pending_confirmations") or {}
        if cmd.payload.slot in pending:
            return None
        return Rejection(
            self.name,
            "confidence_below_threshold",
            cmd.kind,
            f"confidence {cmd.confidence:.2f} < {self.threshold}",
            extra={
                # Keep original_confidence for telemetry/audit; transform
                # carries the canonical rewrite instruction.
                "original_confidence": cmd.confidence,
            },
            transform=Transform(
                to_kind="confirm_slot",
                payload_overrides={
                    "slot": cmd.payload.slot,
                    "proposed_value": cmd.payload.value,
                },
                confidence=1.0,
                rationale="auto_transform_from_set_slot:confidence_below_threshold",
            ),
        )
