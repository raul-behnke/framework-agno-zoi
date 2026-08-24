"""Tools de domínio — resolvidas pelo ``config.yaml`` do tenant.

Um ``tool`` node do routine declara ``ref: agenda_livre``; o ``config.yaml``
diz de qual módulo esse nome vem. O runtime não conhece tool nenhuma por nome:
resolve por configuração, para que vertical nova não exija código.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


class ToolDesconhecida(KeyError):
    """O ``ref`` do nó não está declarado no ``config.yaml`` do tenant."""


def build_registry(config: dict[str, Any]) -> dict[str, Callable[..., Any]]:
    """Resolve ``config['tools']`` em funções chamáveis.

    Cada entrada declara ``module`` e, opcionalmente, ``name`` (default: a
    própria chave). Import que falha é registrado e a tool fica de fora — o
    runtime falha no nó que a usa, não na subida, para que um tenant quebrado
    não derrube os outros.
    """
    registry: dict[str, Callable[..., Any]] = {}
    for ref, spec in (config.get("tools") or {}).items():
        if not isinstance(spec, dict):
            continue
        modulo = spec.get("module")
        if not modulo:
            logger.warning("tools.sem_module ref=%s", ref)
            continue
        nome = spec.get("name") or ref
        try:
            registry[ref] = getattr(importlib.import_module(modulo), nome)
        except (ImportError, AttributeError) as exc:
            logger.warning("tools.import_falhou ref=%s module=%s err=%r", ref, modulo, exc)
    return registry


def call(registry: dict[str, Callable[..., Any]], ref: str, args: dict[str, Any]) -> Any:
    """Chama a tool, devolvendo o payload cru para o slot de saída."""
    fn = registry.get(ref)
    if fn is None:
        raise ToolDesconhecida(f"tool {ref!r} não declarada no config.yaml do tenant")
    return fn(**args)
