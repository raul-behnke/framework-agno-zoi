"""SayScopeRule — say_freetalk only in FreeTalk/End nodes.

FIND-033 — EndNode farewell precedence.

When the current node is an EndNode with a non-empty ``farewell`` template,
the rule REJECTS a LLM-generated ``say_freetalk`` with code
``say_overrides_authored_farewell``. The PlanExecutor's End-node render
path then surfaces the authored copy as the outgoing bubble. Rationale:
the farewell is operator-authored content that must not be replaced by
stochastic LLM output. An EndNode WITHOUT an authored farewell continues
to accept LLM-generated say_freetalk (the bot says "goodbye" of its own).
"""

from __future__ import annotations

from typing import Any

from zoi_agno.contracts import Command
from zoi_agno.enforcement.rule import Rejection

_ALLOWED_KINDS = {"freetalk", "end"}


class SayScopeRule:
    name = "say_scope"

    async def check(
        self,
        cmd: Command,
        state: dict[str, Any],
        ctx: dict[str, Any],
    ) -> Rejection | None:
        if cmd.kind != "say_freetalk":
            return None
        node = ctx.get("current_node_def") or {}
        kind = node.get("kind")
        if kind not in _ALLOWED_KINDS:
            return Rejection(
                self.name,
                "say_outside_freetalk_or_end",
                cmd.kind,
                f"say_freetalk not allowed in node kind={kind!r}",
            )
        # FIND-033 — EndNode farewell precedence. If the End has an authored
        # farewell template, the LLM bubble must not override it.
        if kind == "end":
            farewell = node.get("farewell")
            if farewell:
                return Rejection(
                    self.name,
                    "say_overrides_authored_farewell",
                    cmd.kind,
                    "EndNode has authored farewell — say_freetalk overrides "
                    "operator-authored content",
                    extra={"farewell_preview": str(farewell)[:80]},
                )
        return None
