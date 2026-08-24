"""SkipCollectRateLimitRule — cap consecutive skip_collect commands.

Wave 2 Plan A C5 (UAT round 3+5): bot emitted 12 consecutive ``skip_collect``
in S05 round 3 (2 residual round 5). When cmd-gen drifts into a skip loop,
no existing rule stopped it. Counter ``state["_skip_collect_count"]`` is
maintained by ``turn_manager._stage_apply_side_effects`` (incremented on
accept of skip_collect; reset to 0 on accept of any other command kind).
Rule rejects hard with code ``excessive_skip_collect`` once counter reaches
the configured threshold (default 3). Kind ``skip_collect`` lives in
``_HARD_FAIL_KINDS`` (enforcement.rule), so a rejection aborts the rest of
the batch — operator/audit immediately surfaces the drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from zoi_agno.contracts import Command
from zoi_agno.enforcement.rule import Rejection

_DEFAULT_THRESHOLD = 3


@dataclass
class SkipCollectRateLimitRule:
    threshold: int = _DEFAULT_THRESHOLD
    name: str = "skip_collect_rate"

    async def check(
        self,
        cmd: Command,
        state: dict[str, Any],
        ctx: dict[str, Any],
    ) -> Rejection | None:
        if cmd.kind != "skip_collect":
            return None
        count = int(state.get("_skip_collect_count", 0))
        if count >= self.threshold:
            return Rejection(
                self.name,
                "excessive_skip_collect",
                cmd.kind,
                (
                    f"skip_collect rejected: {count} consecutive already; "
                    f"threshold={self.threshold}. Use clarify or handoff_human."
                ),
                extra={"count": count, "threshold": self.threshold},
            )
        return None
