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


# ─────────────────── a lista do usuário manda ───────────────────


@pytest.fixture
def com_allowlist(monkeypatch, tmp_path):
    """Isola a config numa raiz descartável."""
    import yaml

    from server import config as config_mod

    def configurar(permitidos):
        (tmp_path / "config.yaml").write_text(
            yaml.safe_dump({"capture": {"allowlist": permitidos}}), encoding="utf-8"
        )
        monkeypatch.setattr(config_mod, "ROOT", tmp_path)
        monkeypatch.setattr(config_mod, "IS_FROZEN", False)
        config_mod.reset_cache()

    yield configurar
    from server import config as config_mod

    config_mod.reset_cache()


def test_servico_fora_da_lista_e_ignorado(api, com_allowlist):
    """O caso real: tirei o Discord das preferências e ele continuava
    sendo gravado, porque a extensão reportava com a lista dela."""
    com_allowlist(["meet", "zoom"])

    corpo = api.post(
        "/api/meeting/state",
        json={"ativa": True, "servico": "discord", "titulo": "Discord | #blog"},
    ).json()

    assert corpo["ativa"] is False


def test_servico_na_lista_e_aceito(api, com_allowlist):
    com_allowlist(["meet", "zoom"])

    corpo = api.post(
        "/api/meeting/state",
        json={"ativa": True, "servico": "meet", "titulo": "Reunião — Google Meet"},
    ).json()

    assert corpo["ativa"] is True


def test_o_titulo_tambem_conta(api, com_allowlist):
    """Um termo pode casar pelo título, não só pelo nome do serviço."""
    com_allowlist(["reunião semanal"])

    corpo = api.post(
        "/api/meeting/state",
        json={"ativa": True, "servico": "jitsi", "titulo": "Reunião semanal"},
    ).json()

    assert corpo["ativa"] is True


def test_relato_recusado_limpa_o_anterior(api, com_allowlist):
    """O caso da tela: o Discord seguia marcado como reunião depois de sair
    da lista, porque o relato antigo continuava vivo até expirar."""
    com_allowlist(["meet"])
    api.post("/api/meeting/state", json={"ativa": True, "servico": "meet"})
    assert api.get("/api/meeting/state").json()["ativa"] is True

    api.post("/api/meeting/state", json={"ativa": True, "servico": "discord"})

    corpo = api.get("/api/meeting/state").json()
    assert corpo["ativa"] is False
    assert corpo["servico"] is None, "o relato anterior deveria ter sido limpo"


def test_sem_lista_aceita_tudo(api, com_allowlist):
    """Fechar por omissão faria perder reunião — o padrão é aceitar."""
    com_allowlist([])

    corpo = api.post(
        "/api/meeting/state", json={"ativa": True, "servico": "qualquer-coisa"}
    ).json()

    assert corpo["ativa"] is True


# ──────────────────────────── modo manual ────────────────────────────


def test_o_padrao_e_automatico(api):
    assert api.get("/api/meeting/state").json()["modo"] == "auto"


def test_modo_sempre_grava_sem_reuniao(api):
    """O botão "gravar agora" precisa funcionar sem reunião nenhuma."""
    corpo = api.put("/api/meeting/mode", json={"modo": "sempre"}).json()

    assert corpo["ativa"] is True
    assert corpo["modo"] == "sempre"


def test_modo_nunca_nao_grava_nem_em_reuniao(api):
    """Escolha explícita vence a detecção: às vezes a reunião é privada."""
    api.post("/api/meeting/state", json={"ativa": True, "servico": "meet"})

    corpo = api.put("/api/meeting/mode", json={"modo": "nunca"}).json()

    assert corpo["ativa"] is False


def test_voltar_ao_automatico_restaura_a_deteccao(api):
    api.post("/api/meeting/state", json={"ativa": True, "servico": "meet"})
    api.put("/api/meeting/mode", json={"modo": "nunca"})
    assert api.get("/api/meeting/state").json()["ativa"] is False

    api.put("/api/meeting/mode", json={"modo": "auto"})

    assert api.get("/api/meeting/state").json()["ativa"] is True


def test_modo_invalido_e_recusado(api):
    assert api.put("/api/meeting/mode", json={"modo": "talvez"}).status_code == 422


def test_o_modo_volta_ao_automatico_ao_reiniciar():
    """Vive em memória de propósito: ninguém quer descobrir semanas depois
    que deixou em "nunca" e perdeu tudo."""
    meeting_state.definir_modo("nunca")
    meeting_state.limpar()   # equivale a reiniciar o servidor

    assert meeting_state.modo_atual() == "auto"


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
