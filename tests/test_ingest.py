"""Testes do pipeline de ingestão."""

from __future__ import annotations

from datetime import datetime, timedelta

from server import db
from server.models import IngestMeta, SegmentStatus, Source
from server.pipeline.ingest import SESSION_GAP, ingest_segment, purge_old_audio


def _meta(uid: str, *, started: datetime | None = None, source: Source = Source.MIC,
          duration_ms: int = 4000) -> IngestMeta:
    return IngestMeta(
        device_id="pc-joao",
        source=source,
        started_at=started or datetime(2026, 7, 25, 10, 0, 0),
        duration_ms=duration_ms,
        client_uid=uid,
    )


def test_ingest_grava_audio_e_cria_segmento(temp_db, tmp_path):
    response = ingest_segment(tmp_path, _meta("uid-00000001"), b"audio-falso")

    assert response.status == SegmentStatus.PENDING
    assert response.duplicate is False

    row = db.get_connection().execute(
        "SELECT audio_path, status FROM segments WHERE id = ?", (response.segment_id,)
    ).fetchone()
    from pathlib import Path

    assert Path(row["audio_path"]).read_bytes() == b"audio-falso"
    assert row["status"] == "pending"


def test_reenvio_do_mesmo_uid_nao_duplica(temp_db, tmp_path):
    """Cliente que reenvia após timeout não pode gerar segmento duplicado."""
    first = ingest_segment(tmp_path, _meta("uid-00000002"), b"a")
    second = ingest_segment(tmp_path, _meta("uid-00000002"), b"a")

    assert second.segment_id == first.segment_id
    assert second.duplicate is True

    count = db.get_connection().execute("SELECT COUNT(*) AS n FROM segments").fetchone()["n"]
    assert count == 1


def test_segmentos_contiguos_compartilham_sessao(temp_db, tmp_path):
    base = datetime(2026, 7, 25, 10, 0, 0)
    a = ingest_segment(tmp_path, _meta("uid-00000003", started=base), b"a")
    b = ingest_segment(
        tmp_path, _meta("uid-00000004", started=base + timedelta(seconds=40)), b"b"
    )

    conn = db.get_connection()
    sessions = {
        conn.execute("SELECT session_id FROM segments WHERE id = ?", (x.segment_id,))
        .fetchone()["session_id"]
        for x in (a, b)
    }
    assert len(sessions) == 1, "segmentos próximos devem cair na mesma sessão"


def test_lacuna_longa_abre_nova_sessao(temp_db, tmp_path):
    base = datetime(2026, 7, 25, 10, 0, 0)
    a = ingest_segment(tmp_path, _meta("uid-00000005", started=base), b"a")
    b = ingest_segment(
        tmp_path, _meta("uid-00000006", started=base + SESSION_GAP + timedelta(minutes=1)), b"b"
    )

    conn = db.get_connection()
    sessions = [
        conn.execute("SELECT session_id FROM segments WHERE id = ?", (x.segment_id,))
        .fetchone()["session_id"]
        for x in (a, b)
    ]
    assert sessions[0] != sessions[1]


def test_fontes_diferentes_ficam_em_sessoes_separadas(temp_db, tmp_path):
    """Mic e áudio do sistema são trilhas independentes, mesmo simultâneas."""
    base = datetime(2026, 7, 25, 10, 0, 0)
    a = ingest_segment(tmp_path, _meta("uid-00000007", started=base, source=Source.MIC), b"a")
    b = ingest_segment(
        tmp_path, _meta("uid-00000008", started=base, source=Source.SYSTEM), b"b"
    )

    conn = db.get_connection()
    sessions = [
        conn.execute("SELECT session_id FROM segments WHERE id = ?", (x.segment_id,))
        .fetchone()["session_id"]
        for x in (a, b)
    ]
    assert sessions[0] != sessions[1]


def test_primeira_transcricao_indexa_no_fts(temp_db, tmp_path):
    """Todo segmento nasce com transcript NULL e recebe texto depois.

    Um trigger de FTS5 que emite 'delete' para uma linha nunca indexada faz o
    SQLite acusar 'database disk image is malformed' — exatamente no caminho
    normal do sistema. Este teste tranca a guarda IS NOT NULL.
    """
    response = ingest_segment(tmp_path, _meta("uid-fts00001"), b"audio")

    with db.transaction() as conn:
        conn.execute(
            "UPDATE segments SET status='done', transcript=? WHERE id = ?",
            ("reunião sobre o orçamento", response.segment_id),
        )

    conn = db.get_connection()
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    # Busca sem acento tem que achar o texto acentuado.
    found = conn.execute(
        "SELECT rowid FROM segments_fts WHERE segments_fts MATCH ?", ("orcamento",)
    ).fetchall()
    assert [r["rowid"] for r in found] == [response.segment_id]


def test_retranscricao_substitui_o_indice(temp_db, tmp_path):
    """Reprocessar um segmento troca o texto indexado, sem deixar o antigo."""
    response = ingest_segment(tmp_path, _meta("uid-fts00002"), b"audio")

    for text in ("primeira versao", "segunda versao"):
        with db.transaction() as conn:
            conn.execute(
                "UPDATE segments SET transcript = ? WHERE id = ?", (text, response.segment_id)
            )

    conn = db.get_connection()
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM segments_fts WHERE segments_fts MATCH ?", ("primeira",)
    ).fetchone()["n"] == 0
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM segments_fts WHERE segments_fts MATCH ?", ("segunda",)
    ).fetchone()["n"] == 1


def test_apagar_segmento_pendente_nao_corrompe_indice(temp_db, tmp_path):
    """Retenção pode apagar um segmento que nunca foi transcrito."""
    response = ingest_segment(tmp_path, _meta("uid-fts00003"), b"audio")

    with db.transaction() as conn:
        conn.execute("DELETE FROM segments WHERE id = ?", (response.segment_id,))

    assert db.get_connection().execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_retencao_apaga_audio_e_mantem_transcricao(temp_db, tmp_path):
    from pathlib import Path

    old = datetime.now() - timedelta(days=40)
    response = ingest_segment(tmp_path, _meta("uid-00000009", started=old), b"antigo")

    with db.transaction() as conn:
        conn.execute(
            "UPDATE segments SET status='done', transcript='conversa antiga' WHERE id = ?",
            (response.segment_id,),
        )
    path = Path(
        db.get_connection()
        .execute("SELECT audio_path FROM segments WHERE id = ?", (response.segment_id,))
        .fetchone()["audio_path"]
    )

    assert purge_old_audio(tmp_path, retention_days=30) == 1
    assert not path.exists()

    row = db.get_connection().execute(
        "SELECT audio_path, transcript FROM segments WHERE id = ?", (response.segment_id,)
    ).fetchone()
    assert row["audio_path"] is None
    assert row["transcript"] == "conversa antiga", "transcrição deve sobreviver ao áudio"


def test_retencao_desligada_nao_apaga_nada(temp_db, tmp_path):
    old = datetime.now() - timedelta(days=400)
    response = ingest_segment(tmp_path, _meta("uid-00000010", started=old), b"antigo")
    with db.transaction() as conn:
        conn.execute("UPDATE segments SET status='done' WHERE id = ?", (response.segment_id,))

    assert purge_old_audio(tmp_path, retention_days=0) == 0


def test_retencao_preserva_segmento_ainda_nao_transcrito(temp_db, tmp_path):
    """Pendente antigo não pode perder o áudio antes de ser transcrito."""
    old = datetime.now() - timedelta(days=40)
    ingest_segment(tmp_path, _meta("uid-00000011", started=old), b"pendente")

    assert purge_old_audio(tmp_path, retention_days=30) == 0
