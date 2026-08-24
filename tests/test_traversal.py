"""Aceite da Fase 3: o executor leva uma routine real do início ao fim.

Sem LLM, sem rede, sem banco. Se estes passam, o grafo é navegável por código
determinístico — que é a premissa que separa este runtime de um prompt grande.
"""

from __future__ import annotations

import os

import pytest

from zoi_agno.tenants import list_tenants, load_tenant

from .conftest import FIXTURES_TENANTS
from .driver import NaoTerminou, percorrer

ESTRATEGIAS = range(6)


def test_fixture_chega_a_um_end() -> None:
    t = load_tenant("t_demo", base_dir=FIXTURES_TENANTS)
    r = percorrer(t.routine)
    assert r.end_id.startswith("e_")
    assert r.turnos > 0
    assert r.despedidas, "todo end da fixture tem farewell"


def test_estrategias_diferentes_alcancam_ends_diferentes() -> None:
    """Se todo caminho desemboca no mesmo end, o grafo tem ramo morto."""
    t = load_tenant("t_demo", base_dir=FIXTURES_TENANTS)
    alcancados = {percorrer(t.routine, k=k).end_id for k in ESTRATEGIAS}
    assert len(alcancados) >= 2, f"só um desfecho alcançável: {alcancados}"


def test_nenhuma_estrategia_trava() -> None:
    t = load_tenant("t_demo", base_dir=FIXTURES_TENANTS)
    for k in ESTRATEGIAS:
        percorrer(t.routine, k=k)  # levanta NaoTerminou se travar


# --------------------------------------------------------------------------
# Tenants de produção — o aceite de verdade. Opt-in por variável de ambiente
# porque os artefatos de cliente não moram neste repositório.
# --------------------------------------------------------------------------

_REAIS = os.getenv("ZOI_REAL_TENANTS_DIR")
pytestmark_reais = pytest.mark.skipif(not _REAIS, reason="defina ZOI_REAL_TENANTS_DIR")


@pytestmark_reais
def test_todo_tenant_de_producao_percorre_ate_um_end() -> None:
    for nome in list_tenants(base_dir=_REAIS):
        t = load_tenant(nome, base_dir=_REAIS)
        for k in ESTRATEGIAS:
            try:
                r = percorrer(t.routine, k=k, tenant_id=nome)
            except NaoTerminou as e:
                pytest.fail(f"{nome} (estratégia k={k}): {e}")
            assert r.end_id, f"{nome} k={k} terminou sem id de end"


@pytestmark_reais
def test_travessia_de_producao_e_deterministica() -> None:
    """Mesma estratégia, mesmo desfecho. Sem isso, nada aqui é comparável.

    É a propriedade que o gate da Fase 4 vai depender: se o executor variasse
    entre execuções, comparar v4 e v5 nos mesmos goldens não significaria nada.
    """
    for nome in list_tenants(base_dir=_REAIS):
        t = load_tenant(nome, base_dir=_REAIS)
        for k in ESTRATEGIAS:
            a = percorrer(t.routine, k=k, tenant_id=nome)
            b = percorrer(t.routine, k=k, tenant_id=nome)
            assert a.end_id == b.end_id, f"{nome} k={k}: {a.end_id} != {b.end_id}"
            assert a.visitados == b.visitados, f"{nome} k={k}: caminhos divergiram"


@pytestmark_reais
def test_o_end_alcancado_existe_na_routine() -> None:
    """Trivial de fora, não de dentro: o cursor pode parar num id fantasma."""
    from zoi_routine.ast import EndNode

    for nome in list_tenants(base_dir=_REAIS):
        t = load_tenant(nome, base_dir=_REAIS)
        todos = dict(t.routine.main.nodes)
        for bloco in t.routine.sub_routines.values():
            todos.update(bloco.nodes)
        for k in ESTRATEGIAS:
            r = percorrer(t.routine, k=k, tenant_id=nome)
            assert isinstance(todos.get(r.end_id), EndNode), (
                f"{nome} k={k}: parou em {r.end_id!r}, que não é um end declarado"
            )


# Nota: não afirmamos "N desfechos alcançáveis" para tenants de produção.
# O motorista varia enum e sinal por índice — é um explorador grosseiro, e o
# fato de ele alcançar só um end (caso do imob_sdr, que tem 8) mede a
# estratégia dele, não a navegabilidade do grafo. Cobertura de desfecho real
# vem dos goldens no gate da Fase 4.
