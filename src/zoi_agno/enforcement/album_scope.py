"""AlbumScopeRule — send_album.item_id must be in candidates (anti-fabrication).

Same containment class as SlotScopeRule/appointment_slot_scope: an item_id
cited by the LLM must exist in search_result.candidates. Compact lowercase
compare tolerates 'ap-05' / 'AP05' / 'AP-05' as equivalent.

When search_result is absent (search not yet run) the rule passes through
so as not to block channels that send albums before the main catalog search.
When candidates is an empty list, reject (no valid IDs to cite).

Task 9 (2026-07-17): even when item_id matches a real candidate, reject if
that candidate has no photo (``foto_url`` empty AND ``fotos`` empty/absent)
— distinct code ``item_no_photo`` — so the agent can't try to send a photo
for a photoless product.
"""

from __future__ import annotations

from typing import Any

from zoi_agno.contracts import Command
from zoi_agno.enforcement.rule import Rejection


def _compact(s: Any) -> str:
    """Lowercase + strip hyphens and spaces for fuzzy ID comparison."""
    return str(s or "").strip().lower().replace("-", "").replace(" ", "")


class AlbumScopeRule:
    name = "album_scope"

    async def check(
        self,
        cmd: Command,
        state: dict[str, Any],
        ctx: dict[str, Any],
    ) -> Rejection | None:
        if getattr(cmd, "kind", None) != "send_album":
            return None

        item_id = getattr(getattr(cmd, "payload", None), "item_id", None)

        from zoi_agno.presented import queryable_candidates

        # Validate against the options the lead can currently reference — the
        # live search_result OR the durable _presented_candidates store (a photo
        # request can come from any node/sub after a search; 2026-06-17).
        cands = queryable_candidates(state)
        if not cands:
            # No candidate source at all (no search has run yet) → pass through
            # so channels that send an album before the catalog search aren't
            # blocked. If a search HAS run but returned empty, there are no valid
            # IDs → fall through to reject.
            collected = state.get("collected") or {}
            if (
                collected.get("search_result") is None
                and state.get("_presented_candidates") is None
            ):
                return None

        allowed = {
            _compact(c.get("codigo")) for c in cands if isinstance(c, dict) and c.get("codigo")
        }

        if _compact(item_id) in allowed:
            matched = next(
                (
                    c
                    for c in cands
                    if isinstance(c, dict) and _compact(c.get("codigo")) == _compact(item_id)
                ),
                None,
            )
            if matched is not None and not (matched.get("foto_url") or matched.get("fotos")):
                return Rejection(
                    rule=self.name,
                    code="item_no_photo",
                    command_kind="send_album",
                    detail=(f"send_album item_id {item_id!r} matched a candidate with no photo"),
                    extra={"item_id": item_id},
                )
            return None

        return Rejection(
            rule=self.name,
            code="item_not_in_candidates",
            command_kind="send_album",
            detail=(f"send_album item_id {item_id!r} not in search_result candidates"),
            extra={"item_id": item_id, "candidate_count": len(cands)},
        )
