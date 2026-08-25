"""Como um nó ``wait`` vira uma espera pendente.

Três modos, cada um com um gatilho:

``mode: user``    espera o lead responder, com prazo. Vencido, vai para
                  ``on_timeout``. É o follow-up: "te chamo mais tarde".
``mode: time``    espera até um instante calculado do estado
                  (``fire_at_template``). Vencido, ``on_timeout``.
``mode: signal``  espera um evento externo no tópico declarado. Chegando,
                  vai para ``on_signal``. Sem prazo.

Um ``boundary`` de timer no mesmo nó é um segundo prazo, mais curto ou mais
longo, com destino próprio — quem vence primeiro manda.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from zoi_routine.ast import WaitNode

logger = logging.getLogger(__name__)

_ISO_DUR = re.compile(r"^P(?:(?P<d>\d+)D)?(?:T(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+)S)?)?$")


class DuracaoInvalida(ValueError):
    """A duração declarada não é ISO-8601 reconhecível."""


def duracao(iso: str) -> timedelta:
    """``PT24H`` → 24 horas. Só a forma que o schema do routine aceita."""
    m = _ISO_DUR.match((iso or "").strip().upper())
    if not m or not any(m.groupdict().values()):
        raise DuracaoInvalida(f"duração inválida: {iso!r}")
    p = {k: int(v) for k, v in m.groupdict().items() if v}
    return timedelta(
        days=p.get("d", 0), hours=p.get("h", 0), minutes=p.get("m", 0), seconds=p.get("s", 0)
    )


@dataclass(frozen=True)
class Espera:
    """O que registrar quando a conversa estaciona."""

    node_id: str
    retomar_em: str
    vence_em: datetime | None
    topico: str | None = None


def resolver(
    node: WaitNode, node_id: str, state: dict[str, Any], agora: datetime | None = None
) -> Espera:
    """Traduz o nó em uma espera concreta.

    O prazo mais curto entre o do nó e o de um ``boundary`` de timer é o que
    vale — o boundary existe justamente para cortar antes.
    """
    base = agora or datetime.now(UTC)
    vence: datetime | None = None
    destino = node.on_timeout or node.next

    if node.mode == "signal":
        return Espera(
            node_id=node_id,
            retomar_em=node.on_signal or node.next,
            vence_em=None,
            topico=node.signal_topic,
        )

    if node.mode == "time" and node.fire_at_template:
        vence = _instante_do_template(node.fire_at_template, state) or base
    elif node.timeout:
        vence = base + duracao(node.timeout)

    # Boundary de timer: prazo alternativo com destino próprio.
    for b in node.boundaries or []:
        if b.kind != "timer" or not b.duration:
            continue
        candidato = base + duracao(b.duration)
        if vence is None or candidato < vence:
            vence, destino = candidato, b.target

    return Espera(node_id=node_id, retomar_em=destino, vence_em=vence)


def _instante_do_template(template: str, state: dict[str, Any]) -> datetime | None:
    """Resolve ``{{ lead.data_visita }}`` para um instante.

    Falha vira ``None`` e o chamador cai no prazo padrão: uma data ilegível é
    erro de autoria, e travar a conversa por causa dela é pior que acordar na
    hora errada.
    """
    nome = template.strip().removeprefix("{{").removesuffix("}}").strip()
    nome = nome.removeprefix("lead.").removeprefix("state.").removeprefix("collected.")
    valor = (state.get("collected") or {}).get(nome)
    if not valor:
        return None
    try:
        dt = datetime.fromisoformat(str(valor))
    except ValueError:
        logger.warning("wait.template_ilegivel campo=%s valor=%r", nome, valor)
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
