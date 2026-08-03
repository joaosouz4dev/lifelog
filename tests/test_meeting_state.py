"""Testes do estado de reunião reportado pela extensão.

O TTL é o ponto central: a extensão morre junto com o navegador sem mandar
o "acabou", e sem expiração o gate ficaria aberto para sempre — gravando o
dia inteiro, que é exatamente o que se quer eliminar.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from server import main, meeting_state


@pytest.fixture(autouse=True)
def limpar():
    """O estado é global; sem isto um teste contamina o seguinte."""
    meeting_state.limpar()
    yield
    meeting_state.limpar()


@pytest.fixture
def api():
    return TestClient(main.app)


# ──────────────────────────── o básico ────────────────────────────


def test_sem_relato_nao_ha_reuniao(api):
    corpo = api.get("/api/meeting/state").json()

    assert corpo["ativa"] is False


def test_relato_de_reuniao_fica_ativo(api):
    api.post("/api/meeting/state",
             json={"ativa": True, "servico": "meet", "titulo": "Reunião semanal"})

    corpo = api.get("/api/meeting/state").json()

    assert corpo["ativa"] is True
    assert corpo["servico"] == "meet"
    assert corpo["titulo"] == "Reunião semanal"


def test_relato_de_fim_desativa(api):
    api.post("/api/meeting/state", json={"ativa": True, "servico": "meet"})
    api.post("/api/meeting/state", json={"ativa": False})

    assert api.get("/api/meeting/state").json()["ativa"] is False


def test_post_devolve_o_estado_resultante(api):
    """A extensão confirma o que ficou valendo sem precisar de outro GET."""
    corpo = api.post(
        "/api/meeting/state", json={"ativa": True, "servico": "zoom"}
    ).json()

    assert corpo["ativa"] is True
    assert corpo["servico"] == "zoom"


# ──────────────────────────── o TTL ────────────────────────────


def test_relato_expira_sozinho(monkeypatch):
    """Se a extensão morrer, o gate não pode ficar aberto para sempre."""
    import time

    relogio = {"agora": 1000.0}
    monkeypatch.setattr(meeting_state.time, "monotonic", lambda: relogio["agora"])

    meeting_state.reportar(ativa=True, servico="meet")
    assert meeting_state.atual()["ativa"] is True

    relogio["agora"] += meeting_state.TTL_SEGUNDOS + 1

    assert meeting_state.atual()["ativa"] is False, "deveria ter expirado"


def test_relato_renovado_nao_expira(monkeypatch):
    """A extensão reporta a cada ~10s; renovar mantém o gate aberto."""
    relogio = {"agora": 1000.0}
    monkeypatch.setattr(meeting_state.time, "monotonic", lambda: relogio["agora"])

    meeting_state.reportar(ativa=True, servico="meet")
    for _ in range(5):
        relogio["agora"] += 10
        meeting_state.reportar(ativa=True, servico="meet")

    assert meeting_state.atual()["ativa"] is True


def test_estado_informa_quanto_falta_para_expirar(api):
    """Ajuda a diagnosticar uma extensão que parou de reportar."""
    corpo = api.post("/api/meeting/state", json={"ativa": True}).json()

    assert 0 < corpo["expira_em_s"] <= meeting_state.TTL_SEGUNDOS


# ──────────────────────────── CORS ────────────────────────────


def test_a_extensao_pode_chamar_o_endpoint(api):
    """Sem CORS o navegador bloquearia a extensão em silêncio."""
    r = api.post(
        "/api/meeting/state",
        json={"ativa": True, "servico": "meet"},
        headers={"Origin": "chrome-extension://abcdefghijklmnop"},
    )

    assert r.status_code == 200
    assert "access-control-allow-origin" in {k.lower() for k in r.headers}


def test_site_qualquer_nao_pode_chamar(api):
    """Só extensão: uma página web não deve mexer no estado de gravação."""
    r = api.post(
        "/api/meeting/state",
        json={"ativa": True},
        headers={"Origin": "https://site-qualquer.com"},
    )

    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}
