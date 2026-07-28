"""Testes do filtro na ingestão.

O que está fora da lista de permitidos precisa ser guardado sem consumir
transcrição — visível na timeline, mas sem custo.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from server import db
from server.classify import TITLE_SEP, should_transcribe
from server.models import IngestMeta, SegmentStatus, Source
from server.pipeline import ingest as ingest_mod
from server.pipeline.ingest import ingest_segment


@pytest.fixture
def allowlist(monkeypatch):
    """Configura a lista de permitidos vista pela ingestão."""

    def configurar(permitidos, bloqueados=None):
        def decidir(meta):
            return should_transcribe(
                meta.app_name, meta.source.value,
                allowlist=permitidos, blocklist=bloqueados,
            )

        monkeypatch.setattr(ingest_mod, "_deve_transcrever", decidir)

    return configurar


def _meta(uid: str, app: str | None, source: Source = Source.SYSTEM) -> IngestMeta:
    return IngestMeta(
        device_id="pc-joao",
        source=source,
        started_at=datetime(2026, 7, 27, 10, 0, 0),
        duration_ms=3000,
        client_uid=uid,
        app_name=app,
    )


def _status(segment_id: int) -> str:
    return db.get_connection().execute(
        "SELECT status FROM segments WHERE id = ?", (segment_id,)
    ).fetchone()["status"]


def test_reuniao_permitida_vai_para_transcricao(temp_db, tmp_path, allowlist):
    allowlist(["meet", "zoom"])
    app = f"chrome.exe{TITLE_SEP}Reunião — Google Meet"

    r = ingest_segment(tmp_path, _meta("uid-allow-01", app), b"audio")

    assert r.status is SegmentStatus.PENDING
    assert _status(r.segment_id) == "pending"


def test_video_fora_da_lista_e_guardado_sem_transcrever(temp_db, tmp_path, allowlist):
    """O caso do dia: 135 segmentos de navegador sem valor de relatório."""
    allowlist(["meet", "zoom"])
    app = f"chrome.exe{TITLE_SEP}ESTAGIÁRIO REVELA - YouTube"

    r = ingest_segment(tmp_path, _meta("uid-allow-02", app), b"audio")

    assert _status(r.segment_id) == "skipped"


def test_o_audio_do_segmento_ignorado_e_preservado(temp_db, tmp_path, allowlist):
    """Guardar o áudio permite transcrever depois, se você mudar de ideia."""
    allowlist(["meet"])
    r = ingest_segment(
        tmp_path, _meta("uid-allow-03", f"chrome.exe{TITLE_SEP}Netflix"), b"conteudo"
    )

    row = db.get_connection().execute(
        "SELECT audio_path FROM segments WHERE id = ?", (r.segment_id,)
    ).fetchone()
    from pathlib import Path

    assert Path(row["audio_path"]).read_bytes() == b"conteudo"


def test_microfone_ignora_a_allowlist(temp_db, tmp_path, allowlist):
    """Sua própria voz nunca é filtrada."""
    allowlist(["meet"])

    r = ingest_segment(tmp_path, _meta("uid-allow-04", None, Source.MIC), b"audio")

    assert _status(r.segment_id) == "pending"


def test_motivo_do_descarte_fica_registrado(temp_db, tmp_path, allowlist):
    """Sem o motivo à vista, o segmento sumido vira mistério."""
    allowlist(["meet"])
    r = ingest_segment(
        tmp_path, _meta("uid-allow-05", f"chrome.exe{TITLE_SEP}Twitch"), b"audio"
    )

    erro = db.get_connection().execute(
        "SELECT error FROM segments WHERE id = ?", (r.segment_id,)
    ).fetchone()["error"]
    assert erro == "fora da lista de permitidos"
