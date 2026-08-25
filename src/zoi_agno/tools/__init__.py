"""Tools de domínio — resolvidas pelo ``config.yaml`` do tenant.

Um nó ``tool`` do routine declara ``ref: scooter_search``; o ``config.yaml``
diz o que esse nome é. O runtime não conhece tool nenhuma por nome: resolve
por configuração, para que vertical nova não exija código.

Três formas de declaração:

``kind: catalog``       motor de busca declarativo (filtros, scorers, widen,
                        diversidade, over-budget) sobre o ``catalog.yaml`` do
                        tenant
``kind: ghl_calendar``  agenda — no fixture, um stub determinístico
``kind: python``        função qualquer, via ``module`` + ``name``
"""

from __future__ import annotations

import importlib
import inspect
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class ToolDesconhecida(KeyError):
    """O ``ref`` do nó não está declarado no ``config.yaml`` do tenant."""


def build_registry(config: dict[str, Any], tenant_dir: Path | None = None) -> dict[str, Callable]:
    """Resolve ``config['tools']`` em funções chamáveis.

    Uma tool que falha ao montar fica de fora e o erro aparece no nó que a
    usa — não na subida. Assim um tenant mal configurado não derruba os outros.
    """
    registry: dict[str, Callable] = {}
    for ref, spec in (config.get("tools") or {}).items():
        if not isinstance(spec, dict):
            continue
        kind = str(spec.get("kind") or "python")
        try:
            if kind == "catalog":
                registry[ref] = _montar_catalogo(spec, tenant_dir)
            elif kind == "ghl_calendar":
                registry[ref] = _montar_agenda(spec)
            else:
                registry[ref] = _montar_python(spec, ref)
        except Exception as exc:  # noqa: BLE001 — tenant quebrado não derruba os outros
            logger.warning("tools.montagem_falhou ref=%s kind=%s err=%r", ref, kind, exc)
    return registry


def _montar_python(spec: dict[str, Any], ref: str) -> Callable:
    modulo = spec.get("module")
    if not modulo:
        raise ValueError(f"tool {ref!r} kind=python exige `module`")
    return getattr(importlib.import_module(modulo), spec.get("name") or ref)


def _montar_catalogo(spec: dict[str, Any], tenant_dir: Path | None) -> Callable:
    """O motor de catálogo, com o ``catalog.yaml`` do tenant já ligado.

    A validação da declaração contra o catálogo real acontece aqui, na
    montagem: campo fantasma, coerce desconhecido ou widen morto levantam
    agora, não na primeira conversa.
    """
    from zoi_agno.tools.catalog_search import make_catalog_handler

    if tenant_dir is None:
        raise ValueError("tool kind=catalog exige o diretório do tenant")
    return make_catalog_handler(spec, tenant_dir)


def _montar_agenda(spec: dict[str, Any]) -> Callable:
    """Stub de agenda no lugar do calendário do CRM.

    O fixture não fala com rede. A forma do retorno é a mesma que o
    calendário real produz, então a rule ``appointment_slot_scope`` e o
    grounding funcionam igual.
    """
    from functools import partial

    from zoi_agno.tools.agenda_stub import agenda_livre

    return partial(agenda_livre, max_results=int(spec.get("max_slots") or 4))


async def call(registry: dict[str, Callable], ref: str, args: dict[str, Any]) -> Any:
    """Chama a tool e devolve o payload cru para o slot de saída.

    Aceita handler síncrono ou assíncrono: o motor de catálogo devolve um ou
    outro conforme a fonte, e quem chama não deveria precisar saber.
    """
    fn = registry.get(ref)
    if fn is None:
        raise ToolDesconhecida(f"tool {ref!r} não declarada no config.yaml do tenant")
    resultado = fn(**args)
    if inspect.isawaitable(resultado):
        resultado = await resultado
    return resultado
