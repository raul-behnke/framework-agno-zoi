"""Modelo por papel, com cadeia de fallback.

O ``routing.yaml`` de um tenant declara qual modelo cada cérebro usa e para
quais cair quando o primário falha::

    roles:
      agent:     { primary: gpt-5.4-mini, fallbacks: [deepseek/deepseek-v4-pro, gpt-4o] }
      extractor: { primary: gpt-5.4-mini, fallbacks: [gpt-4o-mini] }

O Agno não tem cadeia de fallback multi-provider nativa — ``retries`` repete o
mesmo modelo. Este módulo supre isso: ``FallbackModel`` tenta os modelos em
ordem e só falha quando todos falharem.

Por que isso importa em produção: o `routing.yaml` do ``sal_imports`` registra
que, com o DeepSeek como primário, saldo zerado degradava o bot em todo turno.
Invertida a ordem, saldo zerado deixa de afetar o serviço e o fallback vira um
kill-switch grátis.
"""

from __future__ import annotations

import logging
from typing import Any

from agno.models.litellm import LiteLLM

logger = logging.getLogger(__name__)

# Papéis que o pipeline conhece. Um tenant pode declarar só alguns; o resto
# cai no default.
PAPEIS = ("agent", "extractor", "freetalk", "perception", "collect", "judge")

# Usado quando o tenant não declara o papel. Deliberadamente conservador: um
# modelo que erra structured output quebra o extrator inteiro.
DEFAULT_PRIMARY = "gpt-5.4-mini"
DEFAULT_FALLBACKS = ("gpt-4o-mini",)


class TodosOsModelosFalharam(RuntimeError):
    """Nenhum modelo da cadeia respondeu."""


class FallbackModel(LiteLLM):
    """``LiteLLM`` que percorre uma cadeia de modelos até um responder.

    Herda de ``LiteLLM`` para satisfazer o contrato de modelo do Agno; o que
    muda é que ``id`` é reatribuído durante a tentativa.
    """

    def __init__(self, *, primary: str, fallbacks: list[str], role: str = "", **kw: Any) -> None:
        super().__init__(id=primary, **kw)
        self._cadeia = [primary, *fallbacks]
        self._role = role

    def _tentar(self, metodo: str, *a: Any, **kw: Any) -> Any:
        erros: list[str] = []
        for i, modelo in enumerate(self._cadeia):
            self.id = modelo
            try:
                return getattr(super(), metodo)(*a, **kw)
            except Exception as exc:  # noqa: BLE001 — a decisão é tentar o próximo
                erros.append(f"{modelo}: {type(exc).__name__}")
                if i + 1 < len(self._cadeia):
                    logger.warning(
                        "gateway.fallback role=%s %s falhou (%s) — tentando %s",
                        self._role,
                        modelo,
                        type(exc).__name__,
                        self._cadeia[i + 1],
                    )
        self.id = self._cadeia[0]  # restaura para a próxima chamada
        raise TodosOsModelosFalharam(f"role={self._role}: {'; '.join(erros)}")

    def invoke(self, *a: Any, **kw: Any) -> Any:
        return self._tentar("invoke", *a, **kw)

    async def ainvoke(self, *a: Any, **kw: Any) -> Any:
        erros: list[str] = []
        for i, modelo in enumerate(self._cadeia):
            self.id = modelo
            try:
                return await super().ainvoke(*a, **kw)
            except Exception as exc:  # noqa: BLE001
                erros.append(f"{modelo}: {type(exc).__name__}")
                if i + 1 < len(self._cadeia):
                    logger.warning(
                        "gateway.fallback role=%s %s falhou (%s) — tentando %s",
                        self._role,
                        modelo,
                        type(exc).__name__,
                        self._cadeia[i + 1],
                    )
        self.id = self._cadeia[0]
        raise TodosOsModelosFalharam(f"role={self._role}: {'; '.join(erros)}")


def modelo_para(role: str, routing: dict[str, Any] | None = None, **kw: Any) -> LiteLLM:
    """Constrói o modelo do papel a partir do ``routing.yaml`` do tenant.

    Sem fallback declarado, devolve um ``LiteLLM`` simples — não vale pagar a
    indireção quando não há cadeia.
    """
    cfg = (routing or {}).get(role) or {}
    primary = str(cfg.get("primary") or DEFAULT_PRIMARY)
    fallbacks = [str(f) for f in (cfg.get("fallbacks") or DEFAULT_FALLBACKS)]
    if not fallbacks:
        return LiteLLM(id=primary, **kw)
    return FallbackModel(primary=primary, fallbacks=fallbacks, role=role, **kw)
