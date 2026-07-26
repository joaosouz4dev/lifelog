"""Testes da listagem de segmentos servida à timeline."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from server import db
from server.models import IngestMeta, Source
from server.pipeline.ingest import ingest_segment

DIA = datetime(2026, 7, 25, 8, 0, 0)


@pytest.fixture
def api(temp_db):
    """Cliente HTTP sobre o banco isolado do teste.

    Sem `with`, de propósito: entrar no contexto dispara o lifespan, que chama
    `db.init()` com o caminho de produção e faz o teste ler o banco real —
    apareciam segmentos de capturas de verdade no meio das asserções. As rotas
    consultadas aqui não precisam do worker que o startup sobe.
    """
    from server import main

    return TestClient(main.app)


def _semear(tmp_path, quantos: int) -> None:
    """Cria segmentos de hora em hora a partir das 08h."""
    for i in range(quantos):
        ingest_segment(
            tmp_path,
            IngestMeta(
                device_id="pc-joao",
                source=Source.MIC,
                started_at=DIA + timedelta(hours=i),
                duration_ms=4000,
                client_uid=f"uid-ordem-{i:04d}",
            ),
            b"audio",
        )


def test_timeline_vem_do_mais_recente_para_o_mais_antigo(api, tmp_path):
    """O que acabou de ser falado precisa aparecer primeiro."""
    _semear(tmp_path, 4)

    corpo = api.get("/api/segments", params={"day": "2026-07-25"}).json()
    horas = [datetime.fromisoformat(s["started_at"]).hour for s in corpo]

    assert horas == [11, 10, 9, 8]
    assert horas == sorted(horas, reverse=True)


def test_limit_corta_os_mais_antigos(api, tmp_path):
    """Num dia longo o recorte que sobra tem de ser o recente, não o do começo."""
    _semear(tmp_path, 5)

    corpo = api.get("/api/segments", params={"day": "2026-07-25", "limit": 2}).json()
    horas = [datetime.fromisoformat(s["started_at"]).hour for s in corpo]

    assert horas == [12, 11]


def test_dia_sem_captura_devolve_lista_vazia(api, tmp_path):
    _semear(tmp_path, 2)

    assert api.get("/api/segments", params={"day": "2026-07-24"}).json() == []
