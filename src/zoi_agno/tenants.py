"""Carregamento dos artefatos de um tenant.

Um tenant é um diretório com sete arquivos YAML, cada um com uma
responsabilidade. Nada em ``zoi_agno/`` conhece o nome de um tenant: vertical
nova é pasta nova, zero linha de Python.

    tenants/<tenant_id>/
      <nome>.routine.yaml   o grafo da conversa      (parseado por zoi-routine)
      persona.yaml          identidade, voz, proibições
      business.yaml         regras que viram código
      config.yaml           tools de domínio (catálogo, agenda)
      routing.yaml          modelo por papel + fallback
      goldens.yaml          conversas do gate de CI
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from zoi_routine import RoutineAst, parse_routine, validate_routine


class TenantNotFoundError(Exception):
    """O diretório do tenant não existe, ou não tem routine."""


def avisos_de_freetalk(ast: RoutineAst) -> list[str]:
    """Freetalk que coleta e não sabe sair.

    Isto NÃO vive no validador do ``zoi-routine``: a regra não é do DSL, é
    deste runtime, onde a saída de um freetalk é por sinal.

    Só o caso inequívoco entra aqui. O inverso — sinais sem slots — é forma
    LEGÍTIMA e comum: ``ft_sem_estoque``, ``ft_objecoes`` e afins existem só
    para rotear, não coletam nada. Avisar sobre eles encheria o boot de
    alarme falso, e alarme falso treina gente a ignorar aviso. Quando um
    freetalk sem slots recebe um ``set_slot`` de verdade — a condição exata
    que congela o cursor — quem avisa é ``pipeline._derivar_sinal_de_escolha``,
    em runtime, com zero falso positivo.
    """
    avisos: list[str] = []
    blocos = [("main", ast.main)] + list(ast.sub_routines.items())
    for escopo, bloco in blocos:
        for node_id, node in bloco.nodes.items():
            if getattr(node, "type", "") != "freetalk":
                continue
            if getattr(node, "slots", None) and not getattr(node, "signals", None):
                onde = node_id if escopo == "main" else f"{escopo}.{node_id}"
                avisos.append(
                    f"freetalk {onde!r} declara slots e nenhum signal: o nó coleta "
                    "mas não tem contrato de saída"
                )
    return avisos


@dataclass(frozen=True)
class Tenant:
    """Os artefatos de um tenant, já parseados."""

    tenant_id: str
    routine: RoutineAst
    persona: dict[str, Any] = field(default_factory=dict)
    business: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    routing: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    dir: Path | None = None
    """Onde o tenant vive — o motor de catálogo lê o catalog.yaml daqui."""

    @property
    def routine_version(self) -> str:
        return self.routine.version_hash or ""

    @property
    def start_node(self) -> str:
        return self.routine.start_node_id


def tenants_dir(explicit: Path | str | None = None) -> Path:
    """Diretório de tenants: argumento > ``ZOI_TENANTS_DIR`` > ``./tenants``."""
    if explicit is not None:
        return Path(explicit)
    return Path(os.getenv("ZOI_TENANTS_DIR", "tenants"))


def _load_yaml(path: Path) -> dict[str, Any]:
    """Lê um YAML opcional. Ausente ou vazio vira ``{}``."""
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def load_tenant(tenant_id: str, *, base_dir: Path | str | None = None) -> Tenant:
    """Carrega e valida um tenant.

    A routine é obrigatória — sem grafo não há agente. Os outros artefatos são
    opcionais: um tenant pode não ter tool de domínio, e o routing cai no
    default do runtime.

    Raises:
        TenantNotFoundError: diretório ausente ou sem ``*.routine.yaml``.
        RoutineParseError / RoutineValidationError: routine inválida.
    """
    root = tenants_dir(base_dir) / tenant_id
    if not root.is_dir():
        raise TenantNotFoundError(f"tenant {tenant_id!r} não encontrado em {root}")

    routines = sorted(root.glob("*.routine.yaml"))
    if not routines:
        raise TenantNotFoundError(f"tenant {tenant_id!r} não tem *.routine.yaml em {root}")
    if len(routines) > 1:
        raise TenantNotFoundError(
            f"tenant {tenant_id!r} tem {len(routines)} routines "
            f"({', '.join(p.name for p in routines)}); esperado exatamente 1"
        )

    ast = parse_routine(routines[0].read_text(encoding="utf-8"), tenant_id=tenant_id)
    warnings = validate_routine(ast) + avisos_de_freetalk(ast)

    return Tenant(
        tenant_id=tenant_id,
        routine=ast,
        persona=_load_yaml(root / "persona.yaml").get("persona", {}),
        business=_load_yaml(root / "business.yaml").get("business", {}),
        config=_load_yaml(root / "config.yaml"),
        routing=_load_yaml(root / "routing.yaml").get("roles", {}),
        warnings=warnings,
        dir=root,
    )


def list_tenants(*, base_dir: Path | str | None = None) -> list[str]:
    """Tenants disponíveis: subdiretório com uma routine, ignorando ``_shared``."""
    root = tenants_dir(base_dir)
    if not root.is_dir():
        return []
    return sorted(
        p.name
        for p in root.iterdir()
        if p.is_dir() and not p.name.startswith("_") and any(p.glob("*.routine.yaml"))
    )
