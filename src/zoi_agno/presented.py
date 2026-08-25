"""Presented-options store — the single source of "options the lead has seen".

Architecture (2026-06-17): once a catalog search has run, the lead can ask for
photos or details of any presented option from ANY node or subflow — not only at
the presentation node, and not only for the picked one. The current-node
``collected.search_result.candidates`` is the live view (and is intentionally
dropped when a Collect-opening sub spawns, to avoid re-presentation); the durable
``_presented_candidates`` channel is the cross-node/cross-sub copy that keeps the
options queryable until the next search supersedes them.

``queryable_candidates(state)`` is the single accessor every photo/detail path
uses (resolve item id, album scope rule, album builder, grounding) so they never
diverge on "which options can the lead reference right now".
"""

from __future__ import annotations

from typing import Any


def queryable_candidates(state: Any) -> list[dict[str, Any]]:
    """Return the catalog candidates the lead can currently reference.

    Union: live ``search_result.candidates`` (current search, first) ∪
    ``_presented_candidates`` (all prior searches, deduped by codigo/id).
    This allows the lead to reference models from previous search rounds
    even after re-searching ("aquela primeira mesmo"). Returns ``[]`` when
    no search has run.
    """
    try:
        collected = state.get("collected") or {}
        sr = collected.get("search_result") or {}
        live = sr.get("candidates") if isinstance(sr, dict) else None
        live = [c for c in live if isinstance(c, dict)] if isinstance(live, list) else []
        pres = state.get("_presented_candidates")
        pres = [c for c in pres if isinstance(c, dict)] if isinstance(pres, list) else []
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for c in [*live, *pres]:  # live (rodada atual) primeiro
            k = str(c.get("codigo") or c.get("id") or "")
            if k and k not in seen:
                seen.add(k)
                out.append(c)
        return out
    except Exception:  # noqa: BLE001 — read helper, never raise into a turn
        return []


_PRESENTED_CAP = 12


def _cand_key(c: dict[str, Any]) -> str:
    return str(c.get("codigo") or c.get("id") or "")


def accumulate_presented(state: Any, candidates: list[dict[str, Any]]) -> None:
    """Funde ``candidates`` no store durável ``_presented_candidates`` (união,
    dedup por codigo/id, NOVOS primeiro, cap ``_PRESENTED_CAP``). Permite o lead
    referenciar modelos de rodadas de busca anteriores ("aquela primeira mesmo")
    mesmo depois de re-buscar. Fail-soft."""
    try:
        fresh = [c for c in (candidates or []) if isinstance(c, dict) and _cand_key(c)]
        prior = [c for c in (state.get("_presented_candidates") or []) if isinstance(c, dict)]
        seen: set[str] = set()
        merged: list[dict[str, Any]] = []
        for c in [*fresh, *prior]:  # novos primeiro
            k = _cand_key(c)
            if k and k not in seen:
                seen.add(k)
                merged.append(c)
        state["_presented_candidates"] = merged[:_PRESENTED_CAP]
    except Exception:  # noqa: BLE001
        return
