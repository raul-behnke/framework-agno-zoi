"""PIIRule v4 — defense-in-depth rejection of say_freetalk payloads with PII.

Primary redaction happens in TurnManager v4 (pre-extractor on user input,
post-Reasoner on outgoing_message). This rule is the third layer: if the
LLM emits a free-text bubble that *still* contains raw PII (e.g. echoing
something from `state.collected` that was prepopulated via CRM reverse
sync without going through redact), reject the command before it reaches
the user.

Only `say_freetalk` is gated. Other command kinds (set_slot, signal, etc.)
do not produce user-visible bubbles directly and are out of scope.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any

from zoi_agno.contracts import Command
from zoi_agno.enforcement.rule import Rejection

logger = logging.getLogger(__name__)


def _build_default_pipeline() -> Any:
    from zoi_agno.presidio_pipeline import PresidioPipeline

    return PresidioPipeline()


@dataclass
class PIIRule:
    """Rejects say_freetalk commands whose text contains detectable PII."""

    name: str = "pii_v4"
    pipeline: Any = field(default=None)
    # FIND-031 — optional metrics recorder so degraded-mode falls visible.
    # When None, the rule still logs once but emits no metric.
    metrics: Any = field(default=None)

    def __post_init__(self) -> None:
        if self.pipeline is None:
            self.pipeline = _build_default_pipeline()
        # Per-instance once-flag (FIND-031). Each PIIRule instance announces
        # degraded mode at most once so dashboards see drift without log spam.
        self._degraded_warned_once: bool = False

    async def check(
        self,
        cmd: Command,
        state: dict[str, Any],
        ctx: dict[str, Any],
    ) -> Rejection | None:
        if cmd.kind != "say_freetalk":
            return None
        text = getattr(cmd.payload, "text", "") or ""
        if not text:
            return None
        try:
            redacted, mapping = self.pipeline.redact(text)
        except Exception as exc:  # noqa: BLE001 — fail-soft, do not block on bug
            # FIND-031 — was a silent ``return None``. Now log once + emit a
            # metric so operators see the rule fell into degraded mode and
            # PII may be slipping through say_freetalk while the upstream
            # PII redact path holds the line.
            if not self._degraded_warned_once:
                self._degraded_warned_once = True
                logger.warning("pii_v4_degraded: %r", exc)
                if self.metrics is not None:
                    with contextlib.suppress(Exception):
                        self.metrics.pii_rule_degraded_mode(
                            tenant=str(state.get("tenant_id", "?") if state else "?")
                        )
            return None
        if not mapping:
            return None
        return Rejection(
            rule=self.name,
            code="pii_in_say_freetalk",
            command_kind=cmd.kind,
            detail=(
                "say_freetalk payload contains PII tokens "
                f"({sorted(mapping.keys())}); redacted preview: {redacted[:120]!r}"
            ),
            extra={"tokens": sorted(mapping.keys())},
        )
