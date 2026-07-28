"""Ingestão de segmentos de áudio.

Grava o arquivo, abre ou reaproveita uma sessão, e enfileira o segmento para
transcrição. É idempotente por `client_uid`: se o cliente reenviar após um
timeout, o segmento existente é devolvido em vez de duplicado.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from .. import db
from ..classify import should_transcribe
from ..models import IngestMeta, IngestResponse, SegmentStatus

log = logging.getLogger(__name__)


def _deve_transcrever(meta: IngestMeta) -> tuple[bool, str]:
    """Consulta a configuração para decidir se este segmento vale transcrição."""
    try:
        from ..config import get_config

        cfg = get_config()
        allowlist = cfg.get("capture.allowlist", None)
        blocklist = cfg.get("capture.blocklist", None)
    except Exception:
        # Sem configuração legível, transcrever é o comportamento seguro:
        # perder uma reunião é pior que gastar com um vídeo.
        return True, "configuração indisponível"

    return should_transcribe(
        meta.app_name, meta.source.value,
        allowlist=allowlist, blocklist=blocklist,
    )

# Uma sessão é reaproveitada enquanto os segmentos chegarem em sequência.
# Uma lacuna maior que isto significa que a captura foi interrompida.
SESSION_GAP = timedelta(minutes=5)

# Assinatura do container Ogg e do cabeçalho Opus dentro dele.
_OGG_MAGIC = b"OggS"
_OPUS_MARKER = b"OpusHead"


class InvalidAudio(ValueError):
    """Áudio que não cumpre o formato do protocolo."""


def validate_audio(payload: bytes) -> None:
    """Rejeita o que não for Opus em container Ogg.

    Sem esta checagem, um cliente que envia WAV ou MP3 recebe 200, o worker
    transcreve lixo e o segmento fica marcado como `done` — indistinguível de
    silêncio legítimo. O erro precisa chegar a quem está escrevendo o cliente,
    não se esconder na timeline.
    """
    if len(payload) < 4:
        raise InvalidAudio("áudio vazio ou truncado")
    if not payload.startswith(_OGG_MAGIC):
        raise InvalidAudio(
            f"esperado container Ogg (assinatura {_OGG_MAGIC!r}), "
            f"veio {payload[:4]!r} — o protocolo exige Opus/Ogg"
        )
    # O OpusHead fica no primeiro pacote; procurar no início do arquivo basta
    # e evita varrer megabytes à toa.
    if _OPUS_MARKER not in payload[:512]:
        raise InvalidAudio("container Ogg sem cabeçalho Opus (codec errado?)")


def _audio_path(data_dir: Path, meta: IngestMeta) -> Path:
    """Caminho particionado por dia — mantém as pastas navegáveis e facilita
    a retenção (apagar um dia é apagar uma pasta)."""
    day = meta.started_at.strftime("%Y-%m-%d")
    directory = data_dir / "audio" / day
    directory.mkdir(parents=True, exist_ok=True)
    stamp = meta.started_at.strftime("%H%M%S")
    return directory / f"{stamp}_{meta.source}_{meta.client_uid}.opus"


def _find_or_create_session(conn: sqlite3.Connection, meta: IngestMeta) -> int:
    """Reaproveita a sessão aberta mais recente da mesma origem, se contígua."""
    cutoff = (meta.started_at - SESSION_GAP).isoformat()
    row = conn.execute(
        """
        SELECT id FROM sessions
         WHERE device_id = ? AND source = ?
           AND COALESCE(ended_at, started_at) >= ?
         ORDER BY started_at DESC LIMIT 1
        """,
        (meta.device_id, meta.source.value, cutoff),
    ).fetchone()

    ends_at = (meta.started_at + timedelta(milliseconds=meta.duration_ms)).isoformat()

    if row is not None:
        conn.execute(
            "UPDATE sessions SET ended_at = ? WHERE id = ? AND (ended_at IS NULL OR ended_at < ?)",
            (ends_at, row["id"], ends_at),
        )
        return int(row["id"])

    cursor = conn.execute(
        """
        INSERT INTO sessions (device_id, source, started_at, ended_at, app_name)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            meta.device_id,
            meta.source.value,
            meta.started_at.isoformat(),
            ends_at,
            meta.app_name,
        ),
    )
    return int(cursor.lastrowid)


def ingest_segment(data_dir: Path, meta: IngestMeta, audio_bytes: bytes) -> IngestResponse:
    """Persiste um segmento recebido e o deixa pronto para transcrição."""
    conn = db.get_connection()

    existing = conn.execute(
        "SELECT id, status FROM segments WHERE client_uid = ?", (meta.client_uid,)
    ).fetchone()
    if existing is not None:
        log.debug("segmento %s já existe (id=%s)", meta.client_uid, existing["id"])
        return IngestResponse(
            segment_id=int(existing["id"]),
            status=SegmentStatus(existing["status"]),
            duplicate=True,
        )

    path = _audio_path(data_dir, meta)
    path.write_bytes(audio_bytes)

    # Decidido na entrada, não na hora de transcrever: assim o segmento fica
    # visível na timeline (com a origem à vista) sem consumir transcrição.
    transcrever, motivo = _deve_transcrever(meta)
    status_inicial = "pending" if transcrever else "skipped"
    if not transcrever:
        log.info("segmento %s ignorado: %s (%s)", meta.client_uid, motivo, meta.app_name)

    try:
        with db.transaction() as tx:
            session_id = _find_or_create_session(tx, meta)
            cursor = tx.execute(
                """
                INSERT INTO segments
                    (session_id, client_uid, started_at, duration_ms, audio_path,
                     app_name, status, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    meta.client_uid,
                    meta.started_at.isoformat(),
                    meta.duration_ms,
                    str(path),
                    # Por segmento: o app muda dentro de uma mesma sessão.
                    meta.app_name,
                    status_inicial,
                    None if transcrever else motivo,
                    ),
            )
            segment_id = int(cursor.lastrowid)
    except sqlite3.IntegrityError:
        # Duas requisições com o mesmo uid em paralelo: a perdedora reencontra
        # a vencedora em vez de estourar erro para o cliente.
        path.unlink(missing_ok=True)
        row = conn.execute(
            "SELECT id, status FROM segments WHERE client_uid = ?", (meta.client_uid,)
        ).fetchone()
        if row is None:
            raise
        return IngestResponse(
            segment_id=int(row["id"]), status=SegmentStatus(row["status"]), duplicate=True
        )
    except Exception:
        path.unlink(missing_ok=True)  # não deixa órfão no disco
        raise

    log.info(
        "segmento %s recebido (%s, %.1fs) -> id=%s",
        meta.client_uid, meta.source.value, meta.duration_ms / 1000, segment_id,
    )
    return IngestResponse(segment_id=segment_id, status=SegmentStatus.PENDING)


def purge_old_audio(data_dir: Path, retention_days: int) -> int:
    """Apaga áudio além do prazo de retenção, preservando a transcrição.

    `retention_days <= 0` desliga a limpeza (áudio mantido para sempre).
    """
    if retention_days <= 0:
        return 0

    cutoff = (datetime.now() - timedelta(days=retention_days)).isoformat()
    conn = db.get_connection()
    rows = conn.execute(
        """
        SELECT id, audio_path FROM segments
         WHERE audio_path IS NOT NULL AND started_at < ? AND status = 'done'
        """,
        (cutoff,),
    ).fetchall()

    removed = 0
    for row in rows:
        try:
            Path(row["audio_path"]).unlink(missing_ok=True)
            with db.transaction() as tx:
                tx.execute("UPDATE segments SET audio_path = NULL WHERE id = ?", (row["id"],))
            removed += 1
        except Exception:
            log.exception("falha ao remover áudio do segmento %s", row["id"])

    if removed:
        log.info("retenção: %s arquivos de áudio removidos", removed)
    return removed
