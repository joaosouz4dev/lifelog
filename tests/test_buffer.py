"""Testes da fila local do cliente.

A fila é o que garante que nada se perde quando o servidor cai ou a rede some.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "windows-client"))

from buffer import MAX_ATTEMPTS, SegmentQueue  # noqa: E402


@pytest.fixture
def queue(tmp_path):
    q = SegmentQueue(tmp_path / "outbox.db", tmp_path / "audio")
    yield q
    q.close()


def _enqueue(q: SegmentQueue, payload: bytes = b"opus", source: str = "mic") -> str:
    return q.enqueue(
        payload, source=source, started_at=datetime.now(), duration_ms=3000
    )


def test_enfileira_e_grava_audio(queue):
    uid = _enqueue(queue, b"conteudo-opus")
    batch = queue.next_batch()

    assert len(batch) == 1
    assert batch[0].client_uid == uid
    assert batch[0].audio_path.read_bytes() == b"conteudo-opus"
    assert queue.stats()["pending"] == 1


def test_envio_remove_da_fila_e_apaga_audio(queue):
    _enqueue(queue)
    segment = queue.next_batch()[0]
    path = segment.audio_path

    queue.mark_sent(segment)

    assert queue.stats()["pending"] == 0
    assert not path.exists(), "áudio local deve sair do disco após o envio"


def test_falha_agenda_nova_tentativa_com_backoff(queue):
    _enqueue(queue)
    segment = queue.next_batch()[0]

    queue.mark_failed(segment, "connection refused")

    # Backoff ativo: o item não deve reaparecer imediatamente…
    assert queue.next_batch() == []
    # …mas continua na fila, com o áudio preservado.
    assert queue.stats()["pending"] == 1
    assert segment.audio_path.exists()


def test_servidor_offline_acumula_e_drena_depois(queue):
    """Cenário real: servidor cai, captura continua, e tudo sobe ao voltar."""
    for _ in range(5):
        _enqueue(queue)

    # Servidor fora: todas as tentativas falham.
    for segment in queue.next_batch(limit=10):
        queue.mark_failed(segment, "connection refused")

    assert queue.stats()["pending"] == 5, "nada pode ser perdido enquanto offline"

    # Servidor volta: zera o backoff e drena.
    with queue._conn:
        queue._conn.execute("UPDATE outbox SET next_try_at = 0")

    drained = queue.next_batch(limit=10)
    assert len(drained) == 5
    for segment in drained:
        queue.mark_sent(segment)

    assert queue.stats()["pending"] == 0


def test_desiste_apos_o_limite_de_tentativas(queue):
    _enqueue(queue)

    for _ in range(MAX_ATTEMPTS):
        with queue._conn:
            queue._conn.execute("UPDATE outbox SET next_try_at = 0")
        batch = queue.next_batch()
        if not batch:
            break
        queue.mark_failed(batch[0], "erro persistente")

    with queue._conn:
        queue._conn.execute("UPDATE outbox SET next_try_at = 0")

    assert queue.next_batch() == [], "item esgotado não deve mais ser tentado"
    assert queue.stats()["stuck"] == 1


def test_rejeicao_definitiva_descarta_o_item(queue):
    """HTTP 4xx não melhora com retry — o item sai da fila."""
    _enqueue(queue)
    segment = queue.next_batch()[0]

    queue.drop_permanently_rejected(segment, "HTTP 422: meta inválido")

    assert queue.stats()["pending"] == 0
    assert not segment.audio_path.exists()


def test_ordem_fifo(queue):
    uids = [_enqueue(queue, f"audio-{i}".encode()) for i in range(3)]
    batch = queue.next_batch(limit=10)

    assert [s.client_uid for s in batch] == uids, "mais antigos primeiro"


def test_fila_sobrevive_a_reinicio(tmp_path):
    """Reiniciar o cliente não pode perder o que ainda não subiu."""
    db, audio = tmp_path / "outbox.db", tmp_path / "audio"

    first = SegmentQueue(db, audio)
    uid = first.enqueue(b"opus", source="mic", started_at=datetime.now(), duration_ms=3000)
    first.close()

    second = SegmentQueue(db, audio)
    try:
        batch = second.next_batch()
        assert len(batch) == 1
        assert batch[0].client_uid == uid
        assert batch[0].audio_path.exists()
    finally:
        second.close()


def test_uids_sao_unicos(queue):
    uids = {_enqueue(queue) for _ in range(50)}
    assert len(uids) == 50
