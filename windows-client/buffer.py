"""Fila local persistente de segmentos.

Tudo que o VAD produz vai primeiro para o disco e só depois é enviado. Se o
servidor estiver fora do ar, a máquina reiniciar ou a rede cair, nada se perde:
a fila é drenada quando a conexão volta.
"""

from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 10
BASE_BACKOFF_SECONDS = 5
MAX_BACKOFF_SECONDS = 600


@dataclass
class QueuedSegment:
    id: int
    client_uid: str
    source: str
    started_at: datetime
    duration_ms: int
    audio_path: Path
    app_name: str | None
    attempts: int


class SegmentQueue:
    """Fila em SQLite com backoff exponencial por item."""

    def __init__(self, db_path: Path, audio_dir: Path):
        self.db_path = db_path
        self.audio_dir = audio_dir
        db_path.parent.mkdir(parents=True, exist_ok=True)
        audio_dir.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA busy_timeout = 10000")
        self._migrate()

    def _migrate(self) -> None:
        with self._conn:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS outbox (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_uid  TEXT    NOT NULL UNIQUE,
                    source      TEXT    NOT NULL,
                    started_at  TEXT    NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    audio_path  TEXT    NOT NULL,
                    app_name    TEXT,
                    attempts    INTEGER NOT NULL DEFAULT 0,
                    next_try_at REAL    NOT NULL DEFAULT 0,
                    last_error  TEXT,
                    -- hora local, como todo timestamp do projeto
                    created_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
                );
                CREATE INDEX IF NOT EXISTS idx_outbox_ready ON outbox(next_try_at, id);
            """)

    # ──────────────────────────── produção ────────────────────────────

    def enqueue(
        self,
        audio_bytes: bytes,
        *,
        source: str,
        started_at: datetime,
        duration_ms: int,
        app_name: str | None = None,
    ) -> str:
        """Grava o áudio e registra o segmento para envio."""
        client_uid = f"{source[:3]}-{uuid.uuid4().hex[:16]}"
        day_dir = self.audio_dir / started_at.strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        path = day_dir / f"{started_at.strftime('%H%M%S')}_{client_uid}.opus"
        path.write_bytes(audio_bytes)

        with self._conn:
            self._conn.execute(
                """
                INSERT INTO outbox
                    (client_uid, source, started_at, duration_ms, audio_path, app_name)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    client_uid,
                    source,
                    started_at.isoformat(),
                    duration_ms,
                    str(path),
                    app_name,
                ),
            )
        return client_uid

    # ──────────────────────────── consumo ────────────────────────────

    def next_batch(self, limit: int = 10) -> list[QueuedSegment]:
        """Itens prontos para envio (respeitando o backoff), mais antigos primeiro."""
        rows = self._conn.execute(
            """
            SELECT * FROM outbox
             WHERE next_try_at <= ? AND attempts < ?
             ORDER BY id LIMIT ?
            """,
            (time.time(), MAX_ATTEMPTS, limit),
        ).fetchall()
        return [
            QueuedSegment(
                id=r["id"],
                client_uid=r["client_uid"],
                source=r["source"],
                started_at=datetime.fromisoformat(r["started_at"]),
                duration_ms=r["duration_ms"],
                audio_path=Path(r["audio_path"]),
                app_name=r["app_name"],
                attempts=r["attempts"],
            )
            for r in rows
        ]

    def revive_stuck(self) -> int:
        """Zera o contador dos itens que esgotaram as tentativas.

        `MAX_ATTEMPTS` protege contra um payload que o servidor nunca vai
        aceitar, mas o mesmo contador também é consumido por indisponibilidade
        — um cliente que passou horas offline esgota as tentativas e passaria a
        descartar áudio legítimo. Chamado quando o servidor volta a responder,
        para que a fila retomada suba inteira.
        """
        with self._conn:
            cursor = self._conn.execute(
                """
                UPDATE outbox SET attempts = 0, next_try_at = 0
                 WHERE attempts >= ?
                """,
                (MAX_ATTEMPTS,),
            )
        if cursor.rowcount:
            log.info("%s segmentos travados voltaram para a fila", cursor.rowcount)
        return cursor.rowcount

    def mark_sent(self, segment: QueuedSegment, *, keep_audio: bool = False) -> None:
        """Remove da fila. O servidor já tem o áudio; a cópia local não é necessária."""
        with self._conn:
            self._conn.execute("DELETE FROM outbox WHERE id = ?", (segment.id,))
        if not keep_audio:
            segment.audio_path.unlink(missing_ok=True)

    def mark_failed(self, segment: QueuedSegment, error: str) -> None:
        """Agenda nova tentativa com backoff exponencial (5s, 10s, 20s… até 10min)."""
        delay = min(BASE_BACKOFF_SECONDS * (2**segment.attempts), MAX_BACKOFF_SECONDS)
        with self._conn:
            self._conn.execute(
                """
                UPDATE outbox
                   SET attempts = attempts + 1, next_try_at = ?, last_error = ?
                 WHERE id = ?
                """,
                (time.time() + delay, error[:500], segment.id),
            )
        if segment.attempts + 1 >= MAX_ATTEMPTS:
            log.error(
                "segmento %s desistiu após %s tentativas: %s",
                segment.client_uid, MAX_ATTEMPTS, error[:200],
            )

    def drop_permanently_rejected(self, segment: QueuedSegment, reason: str) -> None:
        """Descarta itens que o servidor rejeitou de forma definitiva (HTTP 4xx).

        Insistir num payload inválido só entope a fila.
        """
        log.warning("descartando %s: %s", segment.client_uid, reason[:200])
        self.mark_sent(segment)

    # ──────────────────────────── inspeção ────────────────────────────

    def stats(self) -> dict:
        row = self._conn.execute(
            """
            SELECT COUNT(*) AS pending,
                   COALESCE(SUM(attempts >= ?), 0) AS stuck,
                   COALESCE(SUM(duration_ms), 0) AS queued_ms
              FROM outbox
            """,
            (MAX_ATTEMPTS,),
        ).fetchone()
        return {
            "pending": row["pending"],
            "stuck": row["stuck"],
            "queued_seconds": round(row["queued_ms"] / 1000, 1),
        }

    def close(self) -> None:
        self._conn.close()
