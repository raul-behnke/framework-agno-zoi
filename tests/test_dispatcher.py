"""O barramento — quatro políticas entre a intenção e o efeito.

- **soft**  descarta o comando, o lote segue
- **hard**  aborta o resto do lote
- **never** reclama mas aceita — escape universal
- **transform** reescreve o comando e recomeça a fila

A política é decidida pelo *kind* do comando, não pela rule que rejeitou. Uma
rule pode sobrepor só para a sua própria rejeição, via ``force_decision``.
"""

from __future__ import annotations

from typing import Any

from zoi_agno.contracts import Command
from zoi_agno.enforcement.dispatcher_v4 import DispatcherV4
from zoi_agno.enforcement.rule import Rejection, Transform

from .conftest import cmd, ctx


class RuleQueRejeita:
    """Rejeita todo comando de um kind, com política opcional forçada."""

    def __init__(self, kind: str, *, nome: str = "fake", force: str | None = None) -> None:
        self.name = nome
        self._kind = kind
        self._force = force

    async def check(self, c: Command, state: dict[str, Any], _ctx: dict[str, Any]):
        if c.kind != self._kind:
            return None
        return Rejection(
            rule=self.name,
            code="rejeitado_no_teste",
            command_kind=c.kind,
            detail="rejeição sintética",
            force_decision=self._force,  # type: ignore[arg-type]
        )


class RuleQueTransforma:
    name = "transformadora"

    async def check(self, c: Command, state: dict[str, Any], _ctx: dict[str, Any]):
        if c.kind != "set_slot":
            return None
        return Rejection(
            rule=self.name,
            code="coagido",
            command_kind=c.kind,
            detail="vira clarify",
            transform=Transform(
                to_kind="clarify",
                payload_overrides={"question": "pode confirmar?", "options": []},
            ),
        )


async def test_sem_rejeicao_tudo_e_aceito(estado) -> None:
    d = DispatcherV4(rules=[])
    lote = [cmd("set_slot", {"slot": "nome", "value": "Ana"}), cmd("clarify", {"question": "?"})]
    aceitos, rejeicoes = await d.dispatch(lote, estado, ctx())
    assert len(aceitos) == 2 and rejeicoes == []


async def test_soft_dropa_so_o_comando_e_o_lote_segue(estado) -> None:
    """set_slot é soft: o resto do lote sobrevive."""
    d = DispatcherV4(rules=[RuleQueRejeita("set_slot")])
    lote = [
        cmd("set_slot", {"slot": "nome", "value": "Ana"}),
        cmd("clarify", {"question": "e a cidade?"}),
    ]
    aceitos, rejeicoes = await d.dispatch(lote, estado, ctx())
    assert [c.kind for c in aceitos] == ["clarify"]
    assert len(rejeicoes) == 1


async def test_hard_aborta_o_resto_do_lote(estado) -> None:
    """clarify é hard: rejeitá-lo derruba o que vem depois."""
    d = DispatcherV4(rules=[RuleQueRejeita("clarify")])
    lote = [
        cmd("clarify", {"question": "?"}),
        cmd("set_slot", {"slot": "nome", "value": "Ana"}),
    ]
    aceitos, _ = await d.dispatch(lote, estado, ctx())
    assert aceitos == []
    assert estado["_dropped_after_hardbreak"] == ["set_slot"]


async def test_handoff_sobrevive_a_um_hard_break(estado) -> None:
    """A regra mais importante do sistema.

    Se o lead pediu um humano, nenhuma falha anterior no lote pode engolir o
    pedido. O escape é por *kind*, não por posição.
    """
    d = DispatcherV4(rules=[RuleQueRejeita("clarify")])
    lote = [
        cmd("clarify", {"question": "?"}),  # dispara hard break
        cmd("set_slot", {"slot": "nome", "value": "Ana"}),  # engolido
        cmd("handoff_human", {"reason": "quer falar com alguém"}),  # sobrevive
    ]
    aceitos, _ = await d.dispatch(lote, estado, ctx())
    assert [c.kind for c in aceitos] == ["handoff_human"]


async def test_never_aceita_o_comando_apesar_da_rejeicao(estado) -> None:
    d = DispatcherV4(rules=[RuleQueRejeita("handoff_human")])
    aceitos, rejeicoes = await d.dispatch(
        [cmd("handoff_human", {"reason": "pediu vendedor"})], estado, ctx()
    )
    assert [c.kind for c in aceitos] == ["handoff_human"]
    assert len(rejeicoes) == 1, "a rejeição é registrada mesmo assim, para auditoria"


async def test_force_decision_vence_a_politica_never(estado) -> None:
    """É como o FinishFlowGraphRule impede um encerramento prematuro."""
    d = DispatcherV4(rules=[RuleQueRejeita("finish_flow", force="soft")])
    aceitos, _ = await d.dispatch([cmd("finish_flow", {"outcome": "completed"})], estado, ctx())
    assert aceitos == []


async def test_transform_reescreve_o_comando(estado) -> None:
    d = DispatcherV4(rules=[RuleQueTransforma()])
    aceitos, rejeicoes = await d.dispatch(
        [cmd("set_slot", {"slot": "nome", "value": "?"})], estado, ctx()
    )
    assert [c.kind for c in aceitos] == ["clarify"]
    assert aceitos[0].payload.question == "pode confirmar?"
    assert rejeicoes[0].code == "coagido"


async def test_comando_transformado_volta_a_passar_pelas_rules(estado) -> None:
    """A reescrita não é um bypass: o novo comando é fiscalizado também."""
    d = DispatcherV4(rules=[RuleQueTransforma(), RuleQueRejeita("clarify", nome="pega_clarify")])
    aceitos, rejeicoes = await d.dispatch(
        [cmd("set_slot", {"slot": "nome", "value": "?"})], estado, ctx()
    )
    assert aceitos == []
    assert [r.rule for r in rejeicoes] == ["transformadora", "pega_clarify"]


async def test_rejeicoes_sao_gravadas_no_estado(estado) -> None:
    """O redator vê as rejeições do turno — é assim que ele se corrige."""
    d = DispatcherV4(rules=[RuleQueRejeita("set_slot")])
    await d.dispatch([cmd("set_slot", {"slot": "nome", "value": "Ana"})], estado, ctx())
    assert len(estado["enforcement_rejections"]) == 1
    assert estado["enforcement_rejections"][0]["code"] == "rejeitado_no_teste"


async def test_segundo_sinal_do_mesmo_lote_e_barrado(estado) -> None:
    """O contador de sinais do turno é mantido pelo próprio dispatcher."""
    from zoi_agno.enforcement.signal_rate import SignalRateLimitRule

    d = DispatcherV4(rules=[SignalRateLimitRule()])
    c = ctx(node_signals=["escolheu", "desistiu"])
    aceitos, rejeicoes = await d.dispatch(
        [
            cmd("signal", {"name": "escolheu", "value": True}),
            cmd("signal", {"name": "desistiu", "value": True}),
        ],
        estado,
        c,
    )
    assert len(aceitos) == 1
    assert rejeicoes[0].code == "signal_rate_limited"


async def test_fila_real_de_rules_aceita_um_lote_saudavel(estado) -> None:
    """Fumaça de ponta a ponta: as 20 rules juntas não barram o caminho feliz."""
    from zoi_agno.enforcement import default_rules

    estado["_last_user_msg"] = "sou a Ana, de Curitiba"
    d = DispatcherV4(rules=default_rules())
    aceitos, rejeicoes = await d.dispatch(
        [
            cmd("set_slot", {"slot": "nome", "value": "Ana"}),
            cmd("set_slot", {"slot": "cidade", "value": "Curitiba"}),
        ],
        estado,
        ctx(),
    )
    assert len(aceitos) == 2, f"caminho feliz barrado por: {[r.rule for r in rejeicoes]}"
