"""Carregamento de tenant: os sete artefatos YAML viram um objeto Tenant."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from zoi_agno.state import new_session_state, reset_turn
from zoi_agno.tenants import TenantNotFoundError, list_tenants, load_tenant

FIXTURES = Path(__file__).parent / "fixtures" / "tenants"


def test_carrega_o_tenant_de_teste() -> None:
    t = load_tenant("t_demo", base_dir=FIXTURES)
    assert t.tenant_id == "t_demo"
    assert t.routine.routine_name == "demo_barbearia"
    assert t.start_node == "c_abertura"
    assert t.routine_version, "o parser deve carimbar um version_hash"


def test_carrega_persona_business_config_e_routing() -> None:
    t = load_tenant("t_demo", base_dir=FIXTURES)
    assert t.persona["name"] == "Dinho"
    assert "forbidden_phrases" in t.persona
    assert "business_rules" in t.business
    assert t.config["tools"]["agenda_livre"]["kind"] == "python"
    assert t.routing["extractor"]["primary"] == "gpt-5.4-mini"


def test_routine_e_validada_no_carregamento() -> None:
    """Um grafo com referência órfã não deve chegar ao runtime."""
    t = load_tenant("t_demo", base_dir=FIXTURES)
    assert t.warnings == [], f"fixture deveria estar limpa, veio: {t.warnings}"


def test_tenant_inexistente_falha_claro() -> None:
    with pytest.raises(TenantNotFoundError, match="não encontrado"):
        load_tenant("t_fantasma", base_dir=FIXTURES)


def test_lista_tenants_ignora_diretorio_shared() -> None:
    assert "t_demo" in list_tenants(base_dir=FIXTURES)
    assert not any(n.startswith("_") for n in list_tenants(base_dir=FIXTURES))


def test_artefatos_opcionais_ausentes_viram_dicionario_vazio(tmp_path: Path) -> None:
    """Um tenant pode não ter tool de domínio — só a routine é obrigatória."""
    d = tmp_path / "t_minimo"
    d.mkdir()
    (d / "x.routine.yaml").write_text(
        (FIXTURES / "t_demo" / "demo.routine.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    t = load_tenant("t_minimo", base_dir=tmp_path)
    assert t.persona == {} and t.business == {} and t.config == {} and t.routing == {}


# --------------------------------------------------------------------------
# session_state
# --------------------------------------------------------------------------


def test_estado_inicial_aponta_para_o_start_da_routine() -> None:
    t = load_tenant("t_demo", base_dir=FIXTURES)
    st = new_session_state(
        thread_id="tg:123",
        tenant_id=t.tenant_id,
        contact_id="123",
        start_node=t.start_node,
        routine_version=t.routine_version,
    )
    assert st["current_node"] == "c_abertura"
    assert st["collected"] == {}
    assert st["scope"] == "main"


def test_reset_turn_limpa_o_que_e_efemero() -> None:
    """Rejeição de enforcement de turnos atrás não pode vazar pro prompt."""
    st = new_session_state(
        thread_id="t", tenant_id="t_demo", contact_id="c", start_node="c_abertura"
    )
    st["enforcement_rejections"] = [{"rule": "slot_scope"}]
    st["outgoing_message"] = "resposta antiga"
    st["collected"]["nome"] = "Mariana"

    reset_turn(st)

    assert st["enforcement_rejections"] == []
    assert st["outgoing_message"] is None
    assert st["_turn_counter"] == 1
    assert st["collected"]["nome"] == "Mariana", "o que o lead disse não se perde"


# --------------------------------------------------------------------------
# Tenants reais — o teste de migração (Fase 4). Roda só quando apontado.
# --------------------------------------------------------------------------

_REAIS = os.getenv("ZOI_REAL_TENANTS_DIR")


@pytest.mark.skipif(not _REAIS, reason="defina ZOI_REAL_TENANTS_DIR para rodar")
def test_tenants_de_producao_carregam_sem_editar_nada() -> None:
    """A prova de compatibilidade 1:1 com o runtime v4.

    Se um routine de produção carrega aqui sem uma linha alterada, o formato
    de autoria sobreviveu à troca de runtime.
    """
    nomes = list_tenants(base_dir=_REAIS)
    assert nomes, f"nenhum tenant em {_REAIS}"
    for nome in nomes:
        t = load_tenant(nome, base_dir=_REAIS)
        assert t.routine.main.nodes, f"{nome}: routine sem nós"
        assert t.start_node in t.routine.main.nodes
