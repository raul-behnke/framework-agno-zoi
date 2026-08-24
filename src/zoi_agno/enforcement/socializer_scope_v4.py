"""SocializerScopeRule v4 — restricts command kinds during humanization layer.

FIND-030 — DEAD-LETTER pending humanization layer.

This rule is part of spec §7 (12 rules in the v4 enforcement chain). In v3 the
socializer LLM was a separate humanization stage; v4 supersedes that with the
command bus + Reasoner so no caller currently sets ``ctx['in_socializer']``.

The rule is kept in ``_default_rules()`` for spec parity. On construction it
emits a one-time ``enforcement.dead_rule_kept{rule='socializer_scope_v4'}``
metric so dashboards visibilize that an unwired rule is present. Full removal
is a separate spec change tracked outside this hardening pass.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from zoi_agno.contracts import Command
from zoi_agno.enforcement.rule import Rejection

_ALLOWED_IN_SOCIALIZER = {"say_freetalk", "clarify", "handoff_human"}

logger = logging.getLogger(__name__)

# Module-level flag — ensures the dead-rule warning + metric fires once per
# process even if SocializerScopeRule is instantiated many times by tests.
_DEAD_LETTER_ANNOUNCED = False


class SocializerScopeRule:
    name = "socializer_scope_v4"

    def __init__(self, *, metrics: Any = None) -> None:
        """Construct the rule and emit one-time dead-letter signal.

        ``metrics`` is an optional ``MetricsRecorderV4`` instance. When provided
        the rule emits ``enforcement_dead_rule_kept(rule='socializer_scope_v4')``
        the FIRST time this class is constructed in the process. When ``None``
        the metric is skipped but the log line still fires once.
        """
        global _DEAD_LETTER_ANNOUNCED
        if not _DEAD_LETTER_ANNOUNCED:
            _DEAD_LETTER_ANNOUNCED = True
            logger.info(
                "enforcement.socializer_scope_v4 kept as dead-letter "
                "(no caller wires ctx['in_socializer'] in v4)"
            )
            if metrics is not None:
                with contextlib.suppress(Exception):
                    metrics.enforcement_dead_rule_kept(rule=self.name)

    async def check(
        self,
        cmd: Command,
        state: dict[str, Any],
        ctx: dict[str, Any],
    ) -> Rejection | None:
        # No caller sets in_socializer=True in v4, so this is a no-op gate by
        # design (FIND-030). When/if the humanization layer is added, callers
        # will set the flag and the existing scope logic engages.
        if not ctx.get("in_socializer"):
            return None
        if cmd.kind in _ALLOWED_IN_SOCIALIZER:
            return None
        return Rejection(
            self.name,
            "socializer_command_disallowed",
            cmd.kind,
            f"command {cmd.kind!r} not allowed inside socializer scope",
        )
