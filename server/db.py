"""Banco de dados SQLite com migrations incrementais.

Cada migration é uma função que recebe a conexão. A versão aplicada fica em
PRAGMA user_version, então subir o servidor é sempre seguro: só roda o que falta.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

_local = threading.local()
_db_path: Path | None = None


# ─────────────────────────────── migrations ──────────────────────────────


def _m001_initial(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        -- Uma sessão é um período contínuo de captura de uma fonte.
        CREATE TABLE sessions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id    TEXT    NOT NULL,
            source       TEXT    NOT NULL CHECK (source IN ('mic', 'system', 'gadget')),
            started_at   TEXT    NOT NULL,
            ended_at     TEXT,
            app_name     TEXT,
            created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX idx_sessions_started ON sessions(started_at);
        CREATE INDEX idx_sessions_device  ON sessions(device_id, started_at);

        -- Um segmento é um trecho de fala isolado pelo VAD: o "áudio cortado".
        -- status: pending -> transcribing -> done | failed
        CREATE TABLE segments (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id    INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            client_uid    TEXT    NOT NULL,
            started_at    TEXT    NOT NULL,
            duration_ms   INTEGER NOT NULL,
            audio_path    TEXT,
            transcript    TEXT,
            language      TEXT,
            confidence    REAL,
            speaker_label TEXT,
            stt_provider  TEXT,
            cost_cents    REAL    NOT NULL DEFAULT 0,
            status        TEXT    NOT NULL DEFAULT 'pending',
            error         TEXT,
            attempts      INTEGER NOT NULL DEFAULT 0,
            created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
            transcribed_at TEXT
        );
        -- client_uid torna o /ingest idempotente: reenvio após timeout não duplica.
        CREATE UNIQUE INDEX idx_segments_uid     ON segments(client_uid);
        CREATE INDEX        idx_segments_started ON segments(started_at);
        CREATE INDEX        idx_segments_status  ON segments(status, created_at);
        CREATE INDEX        idx_segments_session ON segments(session_id);

        -- Busca por texto exato. sqlite-vec entra na Fase 3 para busca semântica.
        CREATE VIRTUAL TABLE segments_fts USING fts5(
            transcript,
            content='segments',
            content_rowid='id',
            tokenize='unicode61 remove_diacritics 2'
        );

        CREATE TRIGGER segments_fts_insert AFTER INSERT ON segments
        WHEN new.transcript IS NOT NULL BEGIN
            INSERT INTO segments_fts(rowid, transcript) VALUES (new.id, new.transcript);
        END;

        -- A guarda IS NOT NULL é obrigatória: emitir 'delete' para uma linha que
        -- nunca entrou no índice (transcript NULL) faz o FTS5 acusar corrupção.
        -- Todo segmento nasce com transcript NULL, então este é o caminho normal.
        CREATE TRIGGER segments_fts_update AFTER UPDATE OF transcript ON segments BEGIN
            INSERT INTO segments_fts(segments_fts, rowid, transcript)
                SELECT 'delete', old.id, old.transcript WHERE old.transcript IS NOT NULL;
            INSERT INTO segments_fts(rowid, transcript)
                SELECT new.id, new.transcript WHERE new.transcript IS NOT NULL;
        END;

        CREATE TRIGGER segments_fts_delete AFTER DELETE ON segments BEGIN
            INSERT INTO segments_fts(segments_fts, rowid, transcript)
                SELECT 'delete', old.id, old.transcript WHERE old.transcript IS NOT NULL;
        END;

        -- Relatórios diários e mensais (Fase 3).
        CREATE TABLE reports (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            type         TEXT    NOT NULL CHECK (type IN ('daily', 'monthly')),
            period_start TEXT    NOT NULL,
            period_end   TEXT    NOT NULL,
            content_md   TEXT    NOT NULL,
            llm_provider TEXT,
            tokens_in    INTEGER NOT NULL DEFAULT 0,
            tokens_out   INTEGER NOT NULL DEFAULT 0,
            cost_cents   REAL    NOT NULL DEFAULT 0,
            generated_at TEXT    NOT NULL DEFAULT (datetime('now'))
        );
        CREATE UNIQUE INDEX idx_reports_period ON reports(type, period_start);

        -- Intenções detectadas na transcrição. Marcadas agora, executadas depois.
        CREATE TABLE intents (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            segment_id   INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
            type         TEXT    NOT NULL,
            payload_json TEXT    NOT NULL,
            status       TEXT    NOT NULL DEFAULT 'detected',
            created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX idx_intents_status ON intents(status, created_at);

        -- Log de uso dos provedores: alimenta o teto de gasto e o dashboard.
        CREATE TABLE provider_usage (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            hub         TEXT    NOT NULL CHECK (hub IN ('stt', 'llm')),
            provider    TEXT    NOT NULL,
            ok          INTEGER NOT NULL,
            cost_cents  REAL    NOT NULL DEFAULT 0,
            latency_ms  INTEGER,
            error       TEXT,
            created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX idx_usage_day ON provider_usage(created_at, hub);
    """)


MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [
    _m001_initial,
]


# ──────────────────────────────── conexão ────────────────────────────────


def init(db_path: Path) -> None:
    """Define o caminho do banco e aplica as migrations pendentes."""
    global _db_path
    _db_path = db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = _connect(db_path)
    try:
        applied = conn.execute("PRAGMA user_version").fetchone()[0]
        for version in range(applied, len(MIGRATIONS)):
            with conn:
                MIGRATIONS[version](conn)
                conn.execute(f"PRAGMA user_version = {version + 1}")
    finally:
        conn.close()


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # WAL deixa o worker de transcrição escrever enquanto a UI lê.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def get_connection() -> sqlite3.Connection:
    """Conexão por thread — sqlite3 não gosta de compartilhar entre threads."""
    if _db_path is None:
        raise RuntimeError("db.init() precisa ser chamado antes de get_connection()")
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _connect(_db_path)
        _local.conn = conn
    return conn


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """Transação com commit no sucesso e rollback no erro."""
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def close_thread_connection() -> None:
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None
