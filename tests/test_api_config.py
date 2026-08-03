"""Testes dos endpoints que alimentam a tela de configuração."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import yaml
from fastapi.testclient import TestClient

from server import config as config_mod
from server.classify import TITLE_SEP
from server.models import IngestMeta, Source
from server.pipeline.ingest import ingest_segment


@pytest.fixture
def api(temp_db, tmp_path, monkeypatch):
    """Cliente HTTP com a config isolada numa raiz descartável.

    Sem entrar no contexto do TestClient: o lifespan chamaria `db.init()` com
    o caminho de produção e o teste passaria a ler o banco real.
    """
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"capture": {"allowlist": ["meet"], "blocklist": []}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_mod, "ROOT", tmp_path)
    monkeypatch.setattr(config_mod, "IS_FROZEN", False)
    config_mod.reset_cache()

    from server import main

    yield TestClient(main.app)
    config_mod.reset_cache()


def _semear(tmp_path, app: str, quantos: int = 1, source: Source = Source.SYSTEM) -> None:
    base = datetime.now() - timedelta(hours=1)
    for i in range(quantos):
        ingest_segment(
            tmp_path,
            IngestMeta(
                device_id="pc-joao",
                source=source,
                started_at=base + timedelta(seconds=i * 10),
                duration_ms=3000,
                client_uid=f"cfg-{abs(hash(app)) % 10000}-{i:04d}",
                app_name=app,
            ),
            b"audio",
        )


# ────────────────────────────── leitura ──────────────────────────────


def test_devolve_as_listas_em_vigor(api):
    corpo = api.get("/api/config/capture").json()

    assert corpo["allowlist"] == ["meet"]
    assert corpo["blocklist"] == []


def test_apps_detectados_agrupa_pelo_programa(api, tmp_path):
    """Um valor por título de aba devolveria centenas de linhas quase iguais."""
    _semear(tmp_path, f"chrome.exe{TITLE_SEP}Reunião — Google Meet", 3)
    _semear(tmp_path, f"chrome.exe{TITLE_SEP}playlist - YouTube", 2)
    _semear(tmp_path, "zoom.exe", 1)

    corpo = api.get("/api/config/apps-detectados").json()
    por_programa = {g["programa"]: g for g in corpo}

    assert set(por_programa) == {"chrome.exe", "zoom.exe"}
    assert por_programa["chrome.exe"]["segmentos"] == 5


def test_apps_detectados_traz_os_titulos_como_sugestao(api, tmp_path):
    _semear(tmp_path, f"msedge.exe{TITLE_SEP}Reunião — Google Meet", 4)
    _semear(tmp_path, f"msedge.exe{TITLE_SEP}série - Netflix", 1)

    grupo = api.get("/api/config/apps-detectados").json()[0]
    titulos = [t["titulo"] for t in grupo["titulos"]]

    assert titulos[0] == "Reunião — Google Meet", "o mais frequente vem primeiro"
    assert "série - Netflix" in titulos


def test_apps_detectados_mostra_o_efeito_real_do_filtro(api, tmp_path):
    """A tela precisa mostrar o que está valendo, não fazer o usuário deduzir."""
    api.put("/api/config/capture", json={"allowlist": ["zoom"], "blocklist": []})
    _semear(tmp_path, "zoom.exe", 1)
    _semear(tmp_path, "spotify.exe", 1)

    por_programa = {
        g["programa"]: g for g in api.get("/api/config/apps-detectados").json()
    }

    assert por_programa["zoom.exe"]["permitido"] is True
    assert por_programa["spotify.exe"]["permitido"] is False
    assert "permitidos" in por_programa["spotify.exe"]["motivo"]


def test_microfone_aparece_como_sempre_permitido(api, tmp_path):
    """Desmarcar tudo não pode dar a impressão de que a própria voz some."""
    _semear(tmp_path, "obs.exe", 1, source=Source.MIC)

    grupo = api.get("/api/config/apps-detectados").json()[0]

    assert grupo["permitido"] is True
    assert grupo["motivo"] == "microfone"


def test_apps_detectados_ignora_o_que_esta_fora_da_janela(api, tmp_path):
    antigo = datetime.now() - timedelta(days=30)
    ingest_segment(
        tmp_path,
        IngestMeta(
            device_id="pc-joao", source=Source.SYSTEM, started_at=antigo,
            duration_ms=3000, client_uid="cfg-antigo-01", app_name="antigo.exe",
        ),
        b"audio",
    )
    _semear(tmp_path, "recente.exe", 1)

    programas = {g["programa"] for g in api.get("/api/config/apps-detectados?dias=7").json()}

    assert programas == {"recente.exe"}


# ────────────────────────────── gravação ──────────────────────────────


def test_salvar_muda_a_decisao_da_ingestao(api, tmp_path):
    """O teste que importa: a mudança na tela precisa valer já, sem reiniciar."""
    api.put("/api/config/capture", json={"allowlist": ["zoom"], "blocklist": []})
    permitido = ingest_segment(tmp_path, _meta_zoom("cfg-antes"), b"audio")
    assert permitido.status.value == "pending"

    api.put("/api/config/capture", json={"allowlist": ["teams"], "blocklist": []})
    barrado = ingest_segment(tmp_path, _meta_zoom("cfg-depois"), b"audio")

    assert barrado.status.value == "skipped"


def _meta_zoom(uid: str) -> IngestMeta:
    return IngestMeta(
        device_id="pc-joao",
        source=Source.SYSTEM,
        started_at=datetime.now(),
        duration_ms=3000,
        client_uid=uid,
        app_name="zoom.exe",
    )


def test_termo_vazio_e_descartado(api):
    """`"" in alvo` é sempre True — um vazio viraria "permite tudo"."""
    corpo = api.put(
        "/api/config/capture",
        json={"allowlist": ["", "   ", "meet"], "blocklist": []},
    ).json()

    assert corpo["allowlist"] == ["meet"]


def test_termos_sao_normalizados_e_deduplicados(api):
    corpo = api.put(
        "/api/config/capture",
        json={"allowlist": ["Meet", " meet ", "ZOOM"], "blocklist": []},
    ).json()

    assert corpo["allowlist"] == ["meet", "zoom"]


def test_salvar_devolve_o_que_ficou_valendo(api):
    """A tela reexibe o resultado, não o que enviou."""
    corpo = api.put(
        "/api/config/capture", json={"allowlist": ["zoom"], "blocklist": ["1password"]}
    ).json()

    assert corpo == api.get("/api/config/capture").json()
