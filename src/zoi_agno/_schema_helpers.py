"""Shared Pydantic field helpers for v4 command/plan schemas.

``truncate_str`` powers a ``BeforeValidator`` that silently caps an overflowing
LLM string to ``limit`` chars BEFORE the ``Field(max_length=...)`` constraint
runs. Motivation (2026-06-19): DeepSeek V4 in json_object mode does NOT enforce
the JSON schema's ``maxLength``, so it writes verbose ``rationale`` fields that
exceed the 150/200 caps → ``ValidationError`` → planner/cmd-gen fall back to
``reuse_previous_plan`` (lossy). Truncating the internal rationale (audit/debug
only — never user-facing) keeps the plan/command valid. No-op for models that
already respect the cap (e.g. gpt-5.4-mini, which never tripped it).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def truncate_str(limit: int) -> Callable[[Any], Any]:
    """Return a before-validator that hard-caps a str to ``limit`` chars.

    Non-str input passes through untouched (lets the field's own type
    validation raise the real error). Truncation is a silent hard cut — the
    tail of an internal rationale is expendable; a failed plan is not.
    """

    def _validate(value: Any) -> Any:
        if isinstance(value, str) and len(value) > limit:
            return value[:limit]
        return value

    return _validate
