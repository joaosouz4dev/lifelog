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

    def do_GET(self):  # noqa: N802
        """Responde ao /health que o uploader usa para sondar reconexão."""
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status": "ok"}')

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


def test_portaudio_e_uma_instancia_compartilhada():
    """Uma PyAudio() por thread derruba o processo com segfault.

    O modo padrão do cliente abre mic e system ao mesmo tempo; com uma
    instância por trilha, o WASAPI mata o processo em segundos. Reproduzido
    isoladamente com dois PyAudio() e dois streams, sem código do projeto.
    """
    pytest.importorskip("pyaudiowpatch", reason="só existe no Windows")

    import capture

    capture.close_audio()
    try:
        first = capture.get_audio()
        second = capture.get_audio()
        assert first is second, "get_audio() deve devolver sempre a mesma instância"
    finally:
        capture.close_audio()


def test_close_audio_e_idempotente():
    """Chamar duas vezes não pode estourar — o encerramento roda em finally."""
    pytest.importorskip("pyaudiowpatch", reason="só existe no Windows")

    import capture

    capture.get_audio()
    capture.close_audio()
    capture.close_audio()  # não deve levantar


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


def test_fila_travada_por_ausencia_longa_sobe_quando_o_servidor_volta(queue, fake_server):
    """Ficar offline tempo demais não pode custar o áudio já capturado.

    MAX_ATTEMPTS existe para um payload que o servidor nunca aceitaria, mas o
    mesmo contador é consumido por indisponibilidade. Sem revive_stuck(), um
    cliente que passou horas sem rede descartaria fala legítima.
    """
    from buffer import MAX_ATTEMPTS

    server, _ = fake_server
    _fill(queue, 3)

    # Servidor fora do ar tempo suficiente para esgotar as tentativas.
    for _ in range(MAX_ATTEMPTS):
        with queue._conn:
            queue._conn.execute("UPDATE outbox SET next_try_at = 0")
        for segment in queue.next_batch(limit=10):
            queue.mark_failed(segment, "connection refused")

    assert queue.next_batch(limit=10) == [], "os itens devem estar travados"
    assert queue.stats()["stuck"] == 3

    # Servidor volta: o uploader revive a fila ao ver a primeira resposta boa.
    uploader = _drain(queue, f"http://127.0.0.1:{server.server_port}", timeout=15)

    assert uploader.sent_count == 3, "os 3 segmentos travados devem subir"
    assert queue.stats()["pending"] == 0


def test_revive_stuck_nao_mexe_no_que_ainda_tem_tentativa(queue):
    """Só os esgotados voltam — quem está em backoff normal fica como está."""
    _fill(queue, 2)
    segment = queue.next_batch()[0]
    queue.mark_failed(segment, "erro temporário")

    revived = queue.revive_stuck()

    assert revived == 0, "nada esgotou ainda"


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
