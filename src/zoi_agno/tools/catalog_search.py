"""Generic config-driven catalog search motor (`kind: catalog`).

ONE engine that replaces the bespoke `property_search` (imóveis) and
`vehicle_search` (veículos) tools. Behaviour is declared in the tenant's
`config.yaml::tools.<name>` block (`filters` / `scorers` / `widen`) instead of
Python. A new catalog (joias, peças, planos) = write `config.yaml` +
`catalog.yaml`, zero Python.

The motor mirrors the bespoke handlers exactly (copy, not reinvent):

- ``filters`` DISCARD (short-circuit), never score — mirror `property.py:307-313`
  / `vehicle.py:249-256`. A filter whose coerced input is ``None`` is skipped
  (the `max_val is not None` guards).
- ``scorers`` SUM weighted points, rank, never discard — mirror
  `property.py:241-261` / `vehicle.py:202-222`.
- score-gate drops score==0 items only when there's signal — mirror the
  ``has_signal`` gate (`property.py:333-337` / `vehicle.py:277-278`).
- ``widen`` (Bloco 4.2): when `total == 0`, relax exactly ONE auto param per
  iteration by ascending `priority`, re-search, stop at first non-empty —
  identical to `property.py:386-441`.
- ``custom_scorer`` (escape-hatch): optional ``module:func`` resolving to a pure
  ``(item, inputs) -> int`` summed into the declarative score.

Output shape (NÃO divergir — lido por `_route_decide_node`):
    {"candidates": list[dict], "total": int}                          # sem widen
    {"candidates": ..., "total": ..., "widened": bool, "relaxed": [...]}  # com widen

Catalog items are loaded as **schemaless dicts** (arbitrary catalog), never
dataclasses.
"""

from __future__ import annotations

import importlib
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel


class ToolSpec(BaseModel):
    """Descrição de tool para o modelo. Cópia mínima do contrato do v4."""

    name: str
    description: str
    parameters_schema: dict[str, Any]


from zoi_agno.tools.coercers import COERCERS, coerce_value

# Closed registry of op/matcher names (used both at runtime and for spec/CI
# validation in Phase 6).
FILTER_OPS: frozenset[str] = frozenset({"eq", "lte", "gte", "in"})
SCORER_MATCHERS: frozenset[str] = frozenset(
    {"eq", "contains", "in_list", "fuzzy", "fuzzy_multi", "tokens_in_blob", "region", "near", "gte"}
)
WIDEN_OPS: frozenset[str] = frozenset({"scale", "step", "drop_score_gate"})

ItemT = dict[str, Any]
Decl = dict[str, Any]
CustomScorer = Callable[[ItemT, dict[str, Any]], int]


class CatalogSpecError(ValueError):
    """A ``kind: catalog`` declaration is structurally invalid.

    Raised at LOAD/boot time (inside :func:`make_catalog_handler`) — never
    per-call — so a malformed config refuses to build a tool instead of becoming
    a silently-broken one (same failure class as the ``property_search``-morto
    bug, ADR-0006). The message is PRECISE (names the offending tool/field/op),
    not a generic catch-all.
    """


# --------------------------------------------------------------------------- #
# Catalog loading (schemaless)
# --------------------------------------------------------------------------- #
def load_catalog(tenant_dir: Path, *, catalog_key: str) -> list[ItemT]:
    """Lê ``tenant_dir/catalog.yaml`` e retorna itens disponíveis como dicts.

    Espelha `property.py:189-202` mas schemaless: pega ``raw[catalog_key]``,
    filtra ``it.get("disponivel", True)``, retorna lista de dicts (sem
    dataclass).
    """
    path = tenant_dir / "catalog.yaml"
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    items = raw.get(catalog_key, []) or []
    out: list[ItemT] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        if not it.get("disponivel", True):
            continue
        out.append(dict(it))
    return out


# --------------------------------------------------------------------------- #
# Input coercion
# --------------------------------------------------------------------------- #
def _coerced_input(arguments: dict[str, Any], from_input: str, coerce: str | None) -> Any:
    """Read ``arguments[from_input]`` and apply the named coercer (or identity).

    Treats ``None``/``""``/``"null"`` raw values as absent → returns ``None``
    (so a filter/scorer with no input contributes nothing), mirroring the
    `(x or "").strip() or None` idiom in the bespoke handlers.
    """
    raw = arguments.get(from_input)
    if raw is None or raw == "" or raw == "null":
        return None
    return coerce_value(raw, coerce)


def _coerced_input_decl(decl: Decl, arguments: dict[str, Any]) -> Any:
    """Resolve a filter/scorer's coerced input from ``from_input`` OR
    ``from_inputs`` (a list — first non-absent value wins, mirroring the
    ``x or y`` coalesce in the bespoke `property.py:368-370`).
    """
    coerce = decl.get("coerce")
    ignore = decl.get("ignore_values")

    def _maybe_ignore(val: Any) -> Any:
        """Treat sentinel inputs (e.g. ``"qualquer"``) as absent — mirror
        `vehicle.py:243` (``if cat == "qualquer": cat = None``)."""
        if val is None:
            return None
        if isinstance(ignore, list | tuple | set):
            cmp = val.lower().strip() if isinstance(val, str) else val
            for sentinel in ignore:
                s = sentinel.lower().strip() if isinstance(sentinel, str) else sentinel
                if cmp == s:
                    return None
        return val

    inputs = decl.get("from_inputs")
    if isinstance(inputs, list) and inputs:
        for fi in inputs:
            val = _maybe_ignore(_coerced_input(arguments, fi, coerce))
            if val is not None and val != []:
                return val
        return None
    return _maybe_ignore(_coerced_input(arguments, decl["from_input"], coerce))


# --------------------------------------------------------------------------- #
# Filters (hard, short-circuit discard)
# --------------------------------------------------------------------------- #
def _passes_filter(item: ItemT, op: str, field: str, target: Any) -> bool:
    """A single filter op. ``target is None`` ⇒ filter inactive (passes)."""
    if target is None:
        return True
    value = item.get(field)
    if op == "eq":
        if isinstance(value, str) and isinstance(target, str):
            return value.lower().strip() == target.lower().strip()
        return bool(value == target)
    if op == "lte":
        if value is None:
            return False
        return bool(value <= target)
    if op == "gte":
        if value is None:
            return False
        return bool(value >= target)
    if op == "in":
        # item[field] (a value or list) is contained in / overlaps the target set
        if isinstance(target, list | tuple | set):
            if isinstance(value, list | tuple | set):
                return bool(set(value) & set(target))
            return value in target
        return bool(value == target)
    raise ValueError(f"unsupported filter op: {op!r}")


def _resolve_filter_target(item_filter: Decl, arguments: dict[str, Any]) -> Any:
    """Resolve a filter's comparison target: ``const`` or coerced ``from_input``."""
    if "const" in item_filter:
        return item_filter["const"]
    return _coerced_input_decl(item_filter, arguments)


def _apply_filters(
    catalog: list[ItemT], filters: list[Decl], arguments: dict[str, Any]
) -> list[ItemT]:
    out: list[ItemT] = []
    targets = [
        (f["field"], f.get("op", "eq"), _resolve_filter_target(f, arguments)) for f in filters
    ]
    for item in catalog:
        if all(_passes_filter(item, op, field, target) for field, op, target in targets):
            out.append(item)
    return out


# --------------------------------------------------------------------------- #
# Scorer matchers
# --------------------------------------------------------------------------- #
def _match_eq(value: Any, target: Any) -> bool:
    if isinstance(value, str) and isinstance(target, str):
        return value.lower().strip() == target.lower().strip()
    return bool(value == target)


def _match_contains(value: Any, target: Any) -> bool:
    if value is None or target is None:
        return False
    return str(target).lower().strip() in str(value).lower()


def _match_in_list(value: Any, target: Any) -> bool:
    """``target`` (scalar) is present in ``value`` (a list) — `finalidade in alvo`."""
    if value is None or target is None:
        return False
    if isinstance(value, list | tuple | set):
        tgt = str(target).lower().strip()
        return any(str(v).lower().strip() == tgt for v in value)
    return _match_eq(value, target)


def _score_fuzzy(value: Any, target: str) -> int:
    """substring+token fuzzy — de `vehicle.py:169-191`. exact=2, partial=1."""
    if not target or value is None:
        return 0
    q = target.lower().strip()
    v = str(value).lower()
    if q == v:
        return 2
    if q in v or v in q:
        return 1
    return 0


def _score_fuzzy_multi(item: ItemT, target: str, tiers: list[dict[str, Any]]) -> int:
    """Ordered multi-field fuzzy with first-hit-wins — de `vehicle.py:180-191`.

    ``tiers`` = ``[{field, exact, partial}, ...]`` in priority order. Returns the
    score of the FIRST tier that matches (exact > partial within a tier; outer
    order across tiers), mirroring the mutually-exclusive ``return`` ladder of
    ``_matches_modelo`` (modelo-exact 30 > modelo-partial 15 > versao-substring 5).
    A tier may declare only ``partial`` (substring) — e.g. the versao tier.
    """
    if not target:
        return 0
    q = target.lower().strip()
    if not q:
        return 0
    for tier in tiers:
        value = item.get(tier["field"])
        if value is None:
            continue
        v = str(value).lower()
        exact = tier.get("exact")
        partial = tier.get("partial")
        if exact is not None and q == v:
            return int(exact)
        if partial is not None and (q in v or (tier.get("symmetric") and v in q)):
            return int(partial)
    return 0


def _score_tokens_in_blob(item: ItemT, tokens: list[str], blob_fields: list[str]) -> int:
    """+1 per token found in the concatenated blob — de `property.py:227-238`."""
    if not tokens:
        return 0
    parts: list[str] = []
    for bf in blob_fields:
        val = item.get(bf)
        if isinstance(val, list | tuple | set):
            parts.extend(str(x).lower() for x in val)
        elif val is not None:
            parts.append(str(val).lower())
    blob = " ".join(parts)
    score = 0
    for tok in tokens:
        t = str(tok).lower().strip()
        if t and t in blob:
            score += 1
    return score


def _score_region(
    item: ItemT, regiao: str, fields: list[str], tag_fields: list[str] | None = None
) -> int:
    """Multi-field region match — de `property.py:205-224`.

    ``fields`` é a lista de campos ponderados, primeiro = peso forte (bairro),
    segundo = peso médio (cidade). Tokens longos (>=3) dão pontos extras.

    ``tag_fields`` (opcional): campos list-of-tokens onde cada query-token longo
    (>=3) encontrado soma +1 — espelha o ``for t in im.tags`` de
    `property.py:221-223`. Cada token conta UMA vez por tag-field (qualquer item
    da lista contendo o token), igual ao bespoke (que itera tags e soma 1 por
    tag que contém o token — mas o bespoke soma 1 por *tag*, não por token).
    """
    if not regiao or not fields:
        return 0
    q = regiao.lower().strip()
    score = 0
    primary = str(item.get(fields[0], "")).lower()
    secondary = str(item.get(fields[1], "")).lower() if len(fields) > 1 else ""
    if q and q in primary:
        score += 15
    if q and secondary and q in secondary:
        score += 8
    tag_blobs: list[str] = []
    if tag_fields:
        for tf in tag_fields:
            val = item.get(tf)
            if isinstance(val, list | tuple | set):
                tag_blobs.extend(str(x).lower() for x in val)
            elif val is not None:
                tag_blobs.append(str(val).lower())
    for tok in q.split():
        if len(tok) < 3:
            continue
        if tok in primary:
            score += 4
        if secondary and tok in secondary:
            score += 2
        # +1 per tag (list element) containing the token — mirror
        # `property.py:221-223` (sums 1 per tag, can hit multiple tags).
        for tb in tag_blobs:
            if tok in tb:
                score += 1
    return score


def _score_near(value: Any, target: float | None, tiers: list[list[Any]]) -> int:
    """Proximity tiers — de `property.py:256-260` generalizado.

    ``tiers`` = ``[[ratio, points], ...]`` ascending. Item scores the points of
    the FIRST tier whose ``value <= target * ratio``.
    """
    if target is None or value is None or not tiers:
        return 0
    try:
        v = float(value)
        t = float(target)
    except (TypeError, ValueError):
        return 0
    for tier in tiers:
        ratio = float(tier[0])
        points = int(tier[1])
        if v <= t * ratio:
            return points
    return 0


def _score_one(item: ItemT, scorer: Decl, arguments: dict[str, Any]) -> int:
    match = scorer["match"]
    weight = int(scorer.get("weight", 1))
    coerced = _coerced_input_decl(scorer, arguments)

    if match == "eq":
        return (
            weight if (coerced is not None and _match_eq(item.get(scorer["field"]), coerced)) else 0
        )
    if match == "contains":
        return (
            weight
            if (coerced is not None and _match_contains(item.get(scorer["field"]), coerced))
            else 0
        )
    if match == "in_list":
        return (
            weight
            if (coerced is not None and _match_in_list(item.get(scorer["field"]), coerced))
            else 0
        )
    if match == "fuzzy":
        if coerced is None:
            return 0
        return weight * _score_fuzzy(item.get(scorer["field"]), str(coerced))
    if match == "fuzzy_multi":
        if coerced is None:
            return 0
        # weight is folded into the per-tier exact/partial points (not multiplied).
        return _score_fuzzy_multi(item, str(coerced), scorer.get("tiers", []))
    if match == "gte":
        if coerced is None:
            return 0
        value = item.get(scorer["field"])
        if value is None:
            return 0
        try:
            return weight if float(value) >= float(coerced) else 0
        except (TypeError, ValueError):
            return 0
    if match == "tokens_in_blob":
        tokens = (
            coerced if isinstance(coerced, list) else ([] if coerced is None else [str(coerced)])
        )
        return weight * _score_tokens_in_blob(item, tokens, scorer.get("blob", []))
    if match == "region":
        if coerced is None:
            return 0
        fields = scorer.get("fields") or [scorer.get("field", "")]
        return weight * _score_region(item, str(coerced), fields, scorer.get("tag_fields"))
    if match == "near":
        target = coerced if isinstance(coerced, int | float) else None
        return _score_near(item.get(scorer["field"], coerced), target, scorer.get("tiers", []))
    raise ValueError(f"unsupported scorer match: {match!r}")


def _has_signal(scorers: list[Decl], arguments: dict[str, Any]) -> bool:
    """Mirror the bespoke ``has_signal`` gate — true if any non-``near`` scorer
    has a usable (non-None) input.

    ``near`` is a faixa-de-preço proximity boost and ``gte`` is the ano≥ano_min
    boost — NEITHER is a search signal (mirror `property.py:333` /
    `vehicle.py:277`, which gate on fin/reg/caracs and marca/modelo/categoria
    respectively, never on the price/year boosts). Scorers may opt out of the
    signal gate explicitly via ``signal: false``.
    """
    non_signal_matches = {"near", "gte"}
    for s in scorers:
        if s.get("match") in non_signal_matches:
            continue
        if s.get("signal") is False:
            continue
        if _coerced_input_decl(s, arguments) is not None:
            return True
    return False


# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #
def _serialize(item: ItemT, *, fields: list[str] | None, id_field: str) -> ItemT:
    """Project the item for output. ``fields`` None ⇒ drop control-only keys.

    Always preserves ``id_field`` ("nunca invente código": the LLM must echo
    a real id, never fabricate).
    """
    if fields:
        out = {f: item.get(f) for f in fields}
        out.setdefault(id_field, item.get(id_field))
        return out
    # default: full item minus the availability control flag
    return {k: v for k, v in item.items() if k != "disponivel"}


# --------------------------------------------------------------------------- #
# Greedy diversity
# --------------------------------------------------------------------------- #
def _select_diverse(
    ranked: list[tuple[ItemT, int]], *, field: str, min_gap_pct: float, k: int
) -> list[ItemT]:
    """Greedy diversity over a numeric field, relevance-first. Always keeps the
    top-ranked item; adds the next-ranked only if its field differs from EVERY
    already-picked item by >= min_gap_pct (relative). Tops up with the next-best
    remaining so the count reaches k when too few diverse items exist."""
    picked: list[ItemT] = []
    for item, _score in ranked:
        if len(picked) >= k:
            break
        v = item.get(field)
        if not isinstance(v, (int, float)):
            continue
        ok = True
        for p in picked:
            pv = p.get(field)
            if (
                isinstance(pv, (int, float))
                and pv != 0
                and abs(float(v) - float(pv)) / abs(float(pv)) < min_gap_pct
            ):
                ok = False
                break
        if ok or not picked:
            picked.append(item)
    if len(picked) < k:
        for item, _score in ranked:
            if len(picked) >= k:
                break
            if item not in picked:
                picked.append(item)
    return picked[:k]


# --------------------------------------------------------------------------- #
# Superlative-driven sort (característica "a mais X" → ORDENA desc)
# --------------------------------------------------------------------------- #
def _superlative_field(config: Decl, arguments: dict[str, Any]) -> tuple[str, bool] | None:
    """Campo a ordenar quando o texto de um input casa um superlativo.

    Retorna ``(field, pick_min)`` onde ``pick_min=True`` → ordenar ASC (ex.:
    "mais barata"), ``pick_min=False`` → ordenar DESC (ex.: "mais potente").

    Opt-in via ``config['sort'] = {from_input: <slot_texto>}`` (ausente ⇒ None ⇒
    motor inalterado). Reusa os padrões DETERMINÍSTICOS ``_SUPERLATIVE`` de
    ``candidate_pick`` (fonte única; NÃO reescrever) — quando o lead pede "a mais
    potente", o slot textual ``potencia`` casa ``r"\\bmais\\s+(?:potente|...)"`` →
    ``(potencia_w, False)``, e o motor ordena DESC por esse campo (a top entra no
    top-k em vez de ser expulsa pela diversity de preço).

    **Fix Finding 1:** normaliza ``_`` → `` `` antes do match, porque o cmdgen às
    vezes entrega "mais_potente" (snake_case) em vez de "mais potente". Os padrões
    em ``_SUPERLATIVE`` esperam espaço; sem a normalização o regex não casa e a
    diversity de preço corre — expulsando a top e mentindo ao lead.

    **Fix Finding 2:** retorna ``pick_min`` junto com ``field``; o chamador usa
    ``reverse=not pick_min`` para ordenar ASC nos superlativos de mínimo (ex.:
    "mais barata" → ASC por valor_brl). Antes, o sort era sempre DESC e um
    pick_min superlativo mostraria o mais caro como "o mais barato".

    Import lazy de ``_SUPERLATIVE`` (dentro da função) por defesa: ``candidate_pick``
    e ``catalog_search`` ambos vivem no caminho de import do runtime; um import no
    topo do módulo não cria ciclo hoje (``candidate_pick`` só importa stdlib no
    corpo), mas o lazy elimina o risco e custa nada (1 chamada por busca).

    Fail-soft: config ausente/malformada ou input não-textual ⇒ None.
    """
    sort_cfg = config.get("sort")
    if not isinstance(sort_cfg, dict):
        return None
    fi = sort_cfg.get("from_input")
    raw = arguments.get(fi) if fi else None
    if not isinstance(raw, str) or not raw.strip():
        return None
    # Fix Finding 1: normalize snake_case → spaces before matching
    raw_norm = raw.replace("_", " ")
    from zoi_agno.tools.superlatives import _SUPERLATIVE

    for rx, field, pick_min in _SUPERLATIVE:
        if rx.search(raw_norm):
            return field, pick_min
    return None


# --------------------------------------------------------------------------- #
# Core search
# --------------------------------------------------------------------------- #
def _run_search(
    catalog: list[ItemT],
    *,
    config: Decl,
    arguments: dict[str, Any],
    custom_scorer: CustomScorer | None,
    max_results: int,
    drop_score_gate: bool,
) -> list[ItemT]:
    """Filter (hard) → rank (weighted score) → score-gate → cut → serialize."""
    filters = config.get("filters", []) or []
    scorers = config.get("scorers", []) or []
    id_field = config.get("id_field", "id")
    out_fields = config.get("serialize")

    filtered = _apply_filters(catalog, filters, arguments)

    ranked: list[tuple[ItemT, int]] = []
    for item in filtered:
        score = sum(_score_one(item, s, arguments) for s in scorers)
        if custom_scorer is not None:
            score += int(custom_scorer(item, dict(arguments)))
        ranked.append((item, score))
    ranked.sort(key=lambda x: x[1], reverse=True)

    gate_on_signal = _has_signal(scorers, arguments) and not drop_score_gate
    eligible = [(item, s) for item, s in ranked if (s > 0 or not gate_on_signal)]

    # Superlativo de característica ("a mais potente" / "a mais barata"): ordena pelo
    # campo e SUPRIME a diversity de preço (que espalha preço e expulsa a top —
    # errada quando o lead pediu "a mais X"). Posicionado APÓS o score-gate (eligible
    # mantido intacto: não reintroduz score=0) e ANTES da diversity (pula
    # `_select_diverse`). Só troca o critério de ORDEM + corte. Opt-in via config.
    # Finding 2: _superlative_field retorna (field, pick_min); pick_min=True → ASC.
    sort_result = _superlative_field(config, arguments)
    if sort_result is not None:
        sort_field, pick_min = sort_result
        ordered = [(it, s) for it, s in eligible if isinstance(it.get(sort_field), (int, float))]
        ordered.sort(key=lambda x: float(x[0].get(sort_field, 0)), reverse=not pick_min)
        selected = [item for item, _ in ordered[:max_results]]
        return [_serialize(item, fields=out_fields, id_field=id_field) for item in selected]

    div = config.get("diversity")
    if isinstance(div, dict) and div.get("field"):
        selected = _select_diverse(
            eligible,
            field=div["field"],
            min_gap_pct=float(div.get("min_gap_pct", 0.15)),
            k=max_results,
        )
    else:
        selected = [item for item, _ in eligible[:max_results]]
    return [_serialize(item, fields=out_fields, id_field=id_field) for item in selected]


# --------------------------------------------------------------------------- #
# Widen
# --------------------------------------------------------------------------- #
def _resolve_max_results(config: Decl, arguments: dict[str, Any]) -> int:
    spec = config.get("max_results")
    default = 3
    lo, hi = 1, 6
    if isinstance(spec, dict):
        default = int(spec.get("default", 3))
        lo = int(spec.get("min", 1))
        hi = int(spec.get("max", 6))
    raw: Any = arguments.get("max_results")
    if raw is None or raw == "" or raw == "null":
        return max(lo, min(hi, default))
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = default
    return max(lo, min(hi, n))


def catalog_search_handler(
    *,
    catalog: list[ItemT],
    config: Decl,
    custom_scorer: CustomScorer | None = None,
    widen: dict[str, Any] | None = None,
    **arguments: Any,
) -> dict[str, Any]:
    """Generic config-driven search handler (espelha `property.py:341-448`).

    ``catalog`` + ``config`` + ``custom_scorer`` + ``widen`` are bound via
    ``partial`` by :func:`make_catalog_handler`. ``**arguments`` are the
    per-call inputs rendered from the Tool node (always heterogeneous strings
    from Collect; coercers convert them per-field).
    """
    max_results = _resolve_max_results(config, arguments)

    candidates = _run_search(
        catalog,
        config=config,
        arguments=arguments,
        custom_scorer=custom_scorer,
        max_results=max_results,
        drop_score_gate=False,
    )

    if widen is None:
        return {"candidates": candidates, "total": len(candidates)}

    relaxed: list[dict[str, Any]] = []
    widened = False

    if len(candidates) == 0:
        auto_params = [
            (param, rule)
            for param, rule in widen.items()
            if isinstance(rule, dict) and rule.get("auto") is True
        ]
        auto_params.sort(key=lambda x: x[1].get("priority", 1_000_000))

        # Working copy of the per-call arguments; widen mutates the inputs that
        # back the filters/scorers (so re-search picks up the relaxed value).
        cur_args = dict(arguments)
        cur_drop_gate = False

        for param, rule in auto_params:
            op = rule.get("op", "scale")
            from_input = rule.get("from_input")
            coerce = rule.get("coerce")

            if op == "scale":
                if from_input is None:
                    continue
                cur = _coerced_input(cur_args, from_input, coerce)
                if cur is None:
                    continue
                step_pct = float(rule.get("step_pct", 0.0))
                new_val = round(float(cur) * (1 + step_pct))
                attempt = {"param": param, "from": cur, "to": new_val}
                cur_args[from_input] = new_val
            elif op == "step":
                if from_input is None:
                    continue
                cur = _coerced_input(cur_args, from_input, coerce)
                if cur is None:
                    continue
                step = int(rule.get("step", -1))
                new_val = max(0, int(cur) + step)
                attempt = {"param": param, "from": cur, "to": new_val}
                cur_args[from_input] = new_val
            elif op == "drop_score_gate":
                cur_drop_gate = True
                prev = _coerced_input(cur_args, from_input, coerce) if from_input else None
                attempt = {"param": param, "from": prev, "to": f"{prev or 'qualquer'} +1"}
            else:
                raise ValueError(f"unsupported widen op: {op!r}")

            widened = True
            relaxed.append(attempt)

            candidates = _run_search(
                catalog,
                config=config,
                arguments=cur_args,
                custom_scorer=custom_scorer,
                max_results=max_results,
                drop_score_gate=cur_drop_gate,
            )
            if len(candidates) > 0:
                break

    # Over-budget nearest-above fallback (research wf_74abaf17): single pass,
    # NO silent ceiling relaxation. When the hard budget filter leaves nothing,
    # return the cheapest items just above the ceiling, ordered by price gap,
    # flagged over_budget so grounding discloses honestly. Opt-in per tool.
    fb = config.get("over_budget_fallback")
    if len(candidates) == 0 and isinstance(fb, dict):
        field = fb.get("field")
        from_input = fb.get("from_input")
        ceiling = _coerced_input(arguments, from_input, fb.get("coerce")) if from_input else None
        if field and ceiling is not None and float(ceiling) > 0:
            # teto > 0: senão a divisão por ceiling (stretch_pct) levanta
            # ZeroDivisionError → quebra o turno (orçamento "0" do lead/coerção).
            # Drop ONLY the price hard filter; keep category + every other filter.
            fb_config = dict(config)
            fb_config["filters"] = [
                f for f in (config.get("filters") or []) if f.get("field") != field
            ]
            survivors = _apply_filters(catalog, fb_config["filters"], dict(arguments))
            above = [
                it
                for it in survivors
                if isinstance(it.get(field), (int, float)) and float(it[field]) > float(ceiling)
            ]
            above.sort(key=lambda it: float(it[field]))
            interactive = bool(fb.get("interactive"))
            stretch_pct = None
            if interactive and above:
                # Bug A fix: filter PER-ITEM by max_stretch_pct, not just gate on the
                # cheapest. Otherwise a within-stretch cheapest would drag along
                # items far above budget (e.g. ceiling 5000 → 5590 ok, but 6900 +38%
                # is not "logo acima"). All beyond stretch → above=[] → surface
                # nothing (escalate to human).
                max_stretch = float(fb.get("max_stretch_pct", 0.25))
                above = [
                    it
                    for it in above
                    if (float(it[field]) - float(ceiling)) / float(ceiling) <= max_stretch
                ]
            if above:
                stretch_pct = (float(above[0][field]) - float(ceiling)) / float(ceiling)
            k = int(fb.get("max_k", 3))
            id_field = config.get("id_field", "codigo")
            fields = config.get("serialize")
            candidates = [_serialize(it, fields=fields, id_field=id_field) for it in above[:k]]
            if candidates:
                widened = True
                entry = {"param": field, "over_budget": True, "ceiling": float(ceiling)}
                if interactive and stretch_pct is not None:
                    entry["interactive"] = True
                    entry["stretch_pct"] = round(stretch_pct, 4)
                relaxed = [*relaxed, entry]

    # B1 (root-cause 2026-06-27): modelo NOMEADO acima do teto não pode ser
    # escondido. Quando `modelo` casa um item real excluído pelo filtro hard de
    # preço, surface-o flag over_budget (disclosure honesta) ao lado dos in-budget.
    nm_fb = config.get("over_budget_fallback")
    modelo_q = arguments.get("modelo")
    if isinstance(nm_fb, dict) and nm_fb.get("named_model") and modelo_q:
        nm_field = nm_fb.get("field")
        nm_from_input = nm_fb.get("from_input")
        nm_ceiling = (
            _coerced_input(arguments, nm_from_input, nm_fb.get("coerce")) if nm_from_input else None
        )
        nm_id_field = config.get("id_field", "codigo")
        present_codes = {c.get(nm_id_field) for c in candidates}
        if nm_field and nm_ceiling is not None and float(nm_ceiling) > 0:
            # melhor casamento de nome no catálogo inteiro (reusa o scorer `modelo`)
            modelo_tiers: list[dict[str, Any]] = next(
                (
                    s.get("tiers", [])
                    for s in config.get("scorers", [])
                    if s.get("from_input") == "modelo" and s.get("match") == "fuzzy_multi"
                ),
                [],
            )
            best, best_score = None, 0
            for it in catalog:
                sc = _score_fuzzy_multi(it, str(modelo_q), modelo_tiers)
                if sc > best_score:
                    best, best_score = it, sc
            if (
                best is not None
                and best_score > 0
                and best.get(nm_id_field) not in present_codes
                and isinstance(best.get(nm_field), (int, float))
                and float(best[nm_field]) > float(nm_ceiling)
            ):
                nm_fields = config.get("serialize")
                candidates = [
                    _serialize(best, fields=nm_fields, id_field=nm_id_field),
                    *candidates,
                ]
                widened = True
                relaxed = [
                    *relaxed,
                    {
                        "param": nm_field,
                        "over_budget": True,
                        "named_model": best.get(nm_id_field),
                        "ceiling": float(nm_ceiling),
                        "stretch_pct": round(
                            (float(best[nm_field]) - float(nm_ceiling)) / float(nm_ceiling), 4
                        ),
                    },
                ]

    # D-fix (caça 2026-06-27, d_voltz): modelo NOMEADO sem casamento no catálogo
    # não pode virar substituição silenciosa. Quando `modelo` foi pedido mas o
    # widen trouxe alternativas das quais NENHUMA casa o nome, sinalize not_found
    # → o reasoner declara honesto ("não temos exatamente o X, mas..."). Opt-in
    # (`disclose_named_not_found`); sem a flag o comportamento é o de antes.
    if config.get("disclose_named_not_found") and modelo_q and candidates:
        nf_tiers: list[dict[str, Any]] = next(
            (
                s.get("tiers", [])
                for s in config.get("scorers", [])
                if s.get("from_input") == "modelo" and s.get("match") == "fuzzy_multi"
            ),
            [],
        )
        # sem o scorer fuzzy de modelo, _score_fuzzy_multi daria 0 p/ todos →
        # not_found dispararia em TODA busca nomeada (foot-gun p/ futuro tenant).
        if nf_tiers and not any(
            _score_fuzzy_multi(c, str(modelo_q), nf_tiers) > 0 for c in candidates
        ):
            widened = True
            relaxed = [
                *relaxed,
                {"param": "modelo", "not_found": True, "requested": str(modelo_q)},
            ]

    return {
        "candidates": candidates,
        "total": len(candidates),
        "widened": widened,
        "relaxed": relaxed,
    }


# --------------------------------------------------------------------------- #
# Spec generation
# --------------------------------------------------------------------------- #

# widen ops that change a filter's TARGET VALUE (vs merely relaxing its score gate)
_VALUE_RELAX_OPS = frozenset({"scale", "step"})


def _check_widen_no_hard_fields(config: Decl) -> None:
    """Only EXPLICIT hard filters (``hard: true``) may not be value-relaxed in widen.

    A filter entry with ``hard: true`` represents a non-negotiable constraint given
    by the user (e.g. a price ceiling "até X"). A widen op that SCALES/STEPS that
    field's value silently relaxes it — forbidden.

    Filters WITHOUT ``hard: true`` are SOFT preference constraints (e.g. ``min_quartos
    gte``, ``ano_minimo gte``). The user readily compromises on them; relaxing them
    via widen is the desired "relax soft constraints first" behaviour. No error.

    ``drop_score_gate`` widen on ANY field is always allowed (relaxes ranking, not
    the hard cut).
    """
    hard_fields = {f.get("field") for f in (config.get("filters") or []) if f.get("hard") is True}
    hard_inputs = {
        f.get("from_input") for f in (config.get("filters") or []) if f.get("hard") is True
    }
    for param, rule in (config.get("widen") or {}).items():
        if not isinstance(rule, dict) or rule.get("op") not in _VALUE_RELAX_OPS:
            continue
        if param in hard_fields or rule.get("from_input") in (hard_inputs | hard_fields):
            raise CatalogSpecError(
                f"widen[{param!r}] op={rule.get('op')!r} would silently relax the "
                f"HARD-filtered field (hard: true); hard constraints must not be value-relaxed. "
                f"Use an over_budget_fallback (single-pass, explicit) instead."
            )


def _collect_from_inputs(config: Decl) -> list[str]:
    """Unique ordered ``from_input`` names across filters + scorers + widen."""
    seen: dict[str, None] = {}

    def _add(decl: Decl) -> None:
        fi = decl.get("from_input")
        if fi:
            seen.setdefault(fi, None)
        fis = decl.get("from_inputs")
        if isinstance(fis, list):
            for x in fis:
                if x:
                    seen.setdefault(x, None)

    for f in config.get("filters", []) or []:
        _add(f)
    for s in config.get("scorers", []) or []:
        _add(s)
    widen = config.get("widen") or {}
    if isinstance(widen, dict):
        for rule in widen.values():
            if isinstance(rule, dict):
                fi = rule.get("from_input")
                if fi:
                    seen.setdefault(fi, None)
    return list(seen.keys())


_DESCRIPTION_SUFFIX = (
    " Resultado vazio (`total: 0`) significa que NÃO há itens compatíveis — "
    "sinalize ajuste de critério. Nunca invente código/valor/item: use só o "
    "que vier no `candidates`."
)


def build_catalog_spec(decl: Decl) -> ToolSpec:
    """Gera ToolSpec config-aware (anti-pattern: NÃO chamar zero-arg build_spec).

    ``parameters_schema`` deriva dos ``from_input`` únicos de filters+scorers+
    widen (tipo permissivo ``["string","number","null"]`` como hoje) + um
    ``max_results`` inteiro. ``description`` = texto do tenant + nota fixa
    "nunca invente" (espelha `property.py:178-186`).

    Raises :class:`CatalogSpecError` when a widen entry targets a hard-filtered
    field with a value-relax op (scale/step) — fail-loud at load time.
    """
    _check_widen_no_hard_fields(decl)
    properties: dict[str, Any] = {}
    for name in _collect_from_inputs(decl):
        properties[name] = {
            "type": ["string", "number", "null"],
            "description": f"Critério de busca '{name}' (texto livre ou número).",
        }

    mr = decl.get("max_results")
    lo, hi, default = 1, 6, 3
    if isinstance(mr, dict):
        lo = int(mr.get("min", 1))
        hi = int(mr.get("max", 6))
        default = int(mr.get("default", 3))
    properties["max_results"] = {
        "type": "integer",
        "default": default,
        "minimum": lo,
        "maximum": hi,
    }

    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": [],
    }

    base_desc = decl.get("description") or (
        "Consulta o catálogo. Retorna até `max_results` candidatos rankeados "
        "por compatibilidade com o perfil do lead. Filtros duros descartam "
        "itens incompatíveis; o score considera os critérios declarados."
    )
    name = decl.get("name") or "catalog_search"
    return ToolSpec(
        name=name,
        description=str(base_desc) + _DESCRIPTION_SUFFIX,
        parameters_schema=schema,
    )


# --------------------------------------------------------------------------- #
# custom_scorer resolution (privileged Python ref)
# --------------------------------------------------------------------------- #
def _resolve_custom_scorer(ref: str) -> CustomScorer:
    """Import ``modulo:funcao`` → pure ``(item, inputs) -> int`` callable."""
    module_path, _, func_name = ref.partition(":")
    if not module_path or not func_name:
        raise ValueError(f"custom_scorer must be 'module:func', got {ref!r}")
    mod = importlib.import_module(module_path)
    fn = getattr(mod, func_name)
    return fn  # type: ignore[no-any-return]


# --------------------------------------------------------------------------- #
# Spec validation (fail-loud at LOAD/boot — Phase 6)
# --------------------------------------------------------------------------- #
def _catalog_fields(catalog: list[ItemT]) -> set[str]:
    """Union of keys across all catalog items (schemaless → a field present in
    ANY item is legitimate)."""
    fields: set[str] = set()
    for item in catalog:
        fields.update(item.keys())
    return fields


def _declared_from_inputs(decl: Decl) -> set[str]:
    """Every ``from_input``/``from_inputs`` name declared by filters + scorers."""
    names: set[str] = set()
    for f in decl.get("filters", []) or []:
        fi = f.get("from_input")
        if fi:
            names.add(str(fi))
    for s in decl.get("scorers", []) or []:
        fi = s.get("from_input")
        if fi:
            names.add(str(fi))
        fis = s.get("from_inputs")
        if isinstance(fis, list):
            names.update(str(x) for x in fis if x)
    return names


def validate_catalog_decl(decl: Decl, catalog: list[ItemT]) -> None:
    """Fail-loud structural validation of a ``kind: catalog`` declaration.

    Raises :class:`CatalogSpecError` with a PRECISE message when the config is
    invalid. Checks (Phase 6):

    - every filter/scorer ``field`` (and scorer ``fields``/``blob``/``tag_fields``)
      referenced exists in AT LEAST ONE catalog item (phantom-field guard — the
      thing that turns a filter into a silent no-op);
    - every ``coerce`` name is in :data:`COERCERS`;
    - every filter ``op`` is in :data:`FILTER_OPS`, every scorer ``match`` is in
      :data:`SCORER_MATCHERS`, every widen ``op`` is in :data:`WIDEN_OPS`;
    - every widen rule's ``from_input`` corresponds to a declared filter/scorer
      ``from_input`` (priority pointing at a param that no filter/scorer reads is
      dead config).

    Called at LOAD/boot only (:func:`make_catalog_handler`); the per-call path
    stays fail-soft (one bad call must not kill the turn). ``custom_scorer`` /
    ``module`` are NOT validated here (privileged Python refs).
    """
    name = decl.get("name", decl.get("catalog_key", "<unnamed>"))
    fields_present = _catalog_fields(catalog)

    def _check_field(field: Any, *, where: str) -> None:
        if field is None:
            return
        if str(field) not in fields_present:
            raise CatalogSpecError(
                f"catalog tool {name!r}: {where} references field {field!r} which "
                f"exists in NO item of catalog_key={decl.get('catalog_key')!r} "
                f"(known fields: {sorted(fields_present)})"
            )

    def _check_coerce(coerce: Any, *, where: str) -> None:
        if coerce is None:
            return
        if coerce not in COERCERS:
            raise CatalogSpecError(
                f"catalog tool {name!r}: {where} declares unknown coerce "
                f"{coerce!r} (known: {sorted(COERCERS)})"
            )

    # Filters: field present, op known, coerce known.
    for i, f in enumerate(decl.get("filters", []) or []):
        where = f"filter[{i}]"
        if "field" not in f:
            raise CatalogSpecError(f"catalog tool {name!r}: {where} is missing 'field'")
        _check_field(f.get("field"), where=where)
        op = f.get("op", "eq")
        if op not in FILTER_OPS:
            raise CatalogSpecError(
                f"catalog tool {name!r}: {where} declares unknown op {op!r} "
                f"(known: {sorted(FILTER_OPS)})"
            )
        _check_coerce(f.get("coerce"), where=where)

    # Scorers: match known, referenced fields present, coerce known.
    for i, s in enumerate(decl.get("scorers", []) or []):
        where = f"scorer[{i}]"
        match = s.get("match")
        if match not in SCORER_MATCHERS:
            raise CatalogSpecError(
                f"catalog tool {name!r}: {where} declares unknown match {match!r} "
                f"(known: {sorted(SCORER_MATCHERS)})"
            )
        _check_coerce(s.get("coerce"), where=where)
        # Single field (eq/contains/in_list/fuzzy/gte/near/region default).
        _check_field(s.get("field"), where=where)
        # region multi-field weighting.
        for fld in s.get("fields", []) or []:
            _check_field(fld, where=f"{where}.fields")
        # tokens_in_blob blob fields.
        for fld in s.get("blob", []) or []:
            _check_field(fld, where=f"{where}.blob")
        # region tag fields.
        for fld in s.get("tag_fields", []) or []:
            _check_field(fld, where=f"{where}.tag_fields")
        # fuzzy_multi per-tier fields.
        for tier in s.get("tiers", []) or []:
            if isinstance(tier, dict):
                _check_field(tier.get("field"), where=f"{where}.tiers")

    # Widen: op known, from_input wired to a declared filter/scorer input.
    declared_inputs = _declared_from_inputs(decl)
    widen = decl.get("widen") or {}
    if isinstance(widen, dict):
        for key, rule in widen.items():
            if not isinstance(rule, dict):
                continue
            where = f"widen[{key!r}]"
            op = rule.get("op", "scale")
            if op not in WIDEN_OPS:
                raise CatalogSpecError(
                    f"catalog tool {name!r}: {where} declares unknown op {op!r} "
                    f"(known: {sorted(WIDEN_OPS)})"
                )
            _check_coerce(rule.get("coerce"), where=where)
            fi = rule.get("from_input")
            if fi is not None and str(fi) not in declared_inputs:
                raise CatalogSpecError(
                    f"catalog tool {name!r}: {where} from_input {fi!r} does not "
                    f"correspond to any filter/scorer input "
                    f"(declared: {sorted(declared_inputs)})"
                )


# --------------------------------------------------------------------------- #
# Handler factory (used by domain_loader in Phase 2)
# --------------------------------------------------------------------------- #
CatalogSource = Callable[[], "Awaitable[list[ItemT]]"]


def make_catalog_handler(
    decl: Decl,
    tenant_dir: Path,
    *,
    catalog_source: CatalogSource | None = None,
) -> Callable[..., Any]:
    """Build the bound handler for a ``kind: catalog`` tool declaration.

    Two sources, selected by ``catalog_source``:

    - **YAML (default, ``catalog_source is None``)** — unchanged: loads the
      catalog once, **validates the declaration fail-loud against the real
      catalog** (:func:`validate_catalog_decl` — phantom field / unknown
      coerce/op/match / dead widen priority all raise :class:`CatalogSpecError`
      here, at boot, not per-call), and returns a SYNC
      ``partial(catalog_search_handler, catalog=..., ...)``. Mirrors the
      init/widen partial mechanism in `domain_loader.py`.
    - **Live source (``catalog_source`` provided — the ``source: ghl_products``
      opt-in)** — returns an ASYNC handler that awaits ``catalog_source()`` per
      call (the source is itself TTL-cached, see
      um fetcher de fonte viva) and feeds
      the resulting list into the SAME sync search engine. The async handler is
      awaited by the plan executor (``plan_executor.py`` already does
      ``if inspect.isawaitable(result): result = await result``) and the domain
      adapter (``_adapt_handler`` wraps coroutine handlers), so no event-loop
      bridge is needed.

      **Validation trade-off:** :func:`validate_catalog_decl` (the phantom-field
      guard) is SKIPPED for a live source — the item fields are dynamic
      (heuristically parsed from GHL product descriptions), so a field present
      in NO sample at boot is legitimate, not dead config. The op/match/coerce
      sanity is still implicitly enforced per-call by the search engine raising
      on unknown ops; a malformed live decl degrades fail-soft (one bad call,
      not a dead tool).
    """
    custom_scorer: CustomScorer | None = None
    ref = decl.get("custom_scorer")
    if isinstance(ref, str) and ref:
        custom_scorer = _resolve_custom_scorer(ref)

    if catalog_source is not None:
        widen = decl.get("widen")

        async def _live_handler(**arguments: Any) -> dict[str, Any]:
            catalog = await catalog_source()
            return catalog_search_handler(
                catalog=catalog,
                config=decl,
                custom_scorer=custom_scorer,
                widen=widen,
                **arguments,
            )

        return _live_handler

    catalog = load_catalog(tenant_dir, catalog_key=decl["catalog_key"])
    validate_catalog_decl(decl, catalog)
    return partial(
        catalog_search_handler,
        catalog=catalog,
        config=decl,
        custom_scorer=custom_scorer,
        widen=decl.get("widen"),
    )
