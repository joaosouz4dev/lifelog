"""Resiliência do cliente a falhas do servidor.

Os testes de buffer.py cobrem a fila isoladamente; estes exercitam o Uploader
de verdade contra um servidor HTTP que responde (ou não), que é onde o
comportamento real aparece.
"""

from __future__ import annotations

import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "windows-client"))

from buffer import SegmentQueue  # noqa: E402
from uploader import Uploader  # noqa: E402


class _Handler(BaseHTTPRequestHandler):
    """Servidor controlável: responde o status que o teste mandar."""

    status = 200
    received: list[str] = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length", 0))
        self.rfile.read(length)
        type(self).received.append(self.path)

        self.send_response(type(self).status)
        self.send_header("content-type", "application/json")
        self.end_headers()
        body = (
            b'{"segment_id": 1, "status": "pending", "duplicate": false}'
            if type(self).status == 200
            else b'{"detail": "erro"}'
        )
        self.wfile.write(body)

    def log_message(self, *args):  # silencia o log do http.server
        pass


@pytest.fixture
def fake_server():
    _Handler.received = []
    _Handler.status = 200
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, _Handler
    server.shutdown()
    server.server_close()


@pytest.fixture
def queue(tmp_path):
    q = SegmentQueue(tmp_path / "outbox.db", tmp_path / "audio")
    yield q
    q.close()


def _fill(q: SegmentQueue, n: int) -> None:
    for _ in range(n):
        q.enqueue(b"opus-falso", source="mic", started_at=datetime.now(), duration_ms=3000)


def _drain(q: SegmentQueue, url: str, *, timeout: float = 15.0) -> Uploader:
    uploader = Uploader(q, url, "teste", poll_interval=0.2)
    uploader.start()
    deadline = time.time() + timeout
    while time.time() < deadline and q.stats()["pending"] > 0:
        time.sleep(0.2)
    uploader.stop()
    uploader.join(timeout=5)
    return uploader


def test_envia_a_fila_quando_o_servidor_responde(queue, fake_server):
    server, _ = fake_server
    _fill(queue, 3)

    uploader = _drain(queue, f"http://127.0.0.1:{server.server_port}")

    assert uploader.sent_count == 3
    assert queue.stats()["pending"] == 0


def test_servidor_fora_do_ar_retem_tudo(queue):
    """O caso que importa: nada pode se perder enquanto o servidor está morto."""
    _fill(queue, 4)

    # Porta fechada: connection refused imediato.
    uploader = _drain(queue, "http://127.0.0.1:9", timeout=6)

    assert uploader.sent_count == 0
    assert queue.stats()["pending"] == 4, "segmentos não podem sumir com o servidor fora"


def test_fila_drena_quando_o_servidor_volta(queue, fake_server):
    """Ciclo completo: cai, acumula, volta, sobe tudo."""
    server, _ = fake_server
    _fill(queue, 4)

    offline = _drain(queue, "http://127.0.0.1:9", timeout=6)
    assert offline.sent_count == 0
    assert queue.stats()["pending"] == 4

    # Servidor volta; o cliente real esperaria o backoff expirar.
    with queue._conn:
        queue._conn.execute("UPDATE outbox SET next_try_at = 0")

    online = _drain(queue, f"http://127.0.0.1:{server.server_port}")

    assert online.sent_count == 4
    assert queue.stats()["pending"] == 0


def test_erro_definitivo_do_servidor_descarta_o_item(queue, fake_server):
    """422 não melhora com retentativa — insistir entupiria a fila."""
    server, handler = fake_server
    handler.status = 422
    _fill(queue, 2)

    uploader = _drain(queue, f"http://127.0.0.1:{server.server_port}", timeout=10)

    assert uploader.sent_count == 0
    assert queue.stats()["pending"] == 0, "itens rejeitados devem sair da fila"


def test_erro_temporario_do_servidor_mantem_na_fila(queue, fake_server):
    """500 é transitório: o item fica para a próxima tentativa."""
    server, handler = fake_server
    handler.status = 500
    _fill(queue, 2)

    uploader = _drain(queue, f"http://127.0.0.1:{server.server_port}", timeout=6)

    assert uploader.sent_count == 0
    assert queue.stats()["pending"] == 2, "5xx não pode descartar o segmento"
