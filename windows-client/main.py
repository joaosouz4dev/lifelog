"""Ponto de entrada do cliente Windows.

    python main.py                     # captura microfone + áudio do sistema
    python main.py --source mic        # só microfone
    python main.py --list-devices      # diagnóstico de dispositivos

Ctrl+C encerra com segurança: as trilhas são fechadas, o segmento em curso é
salvo e a fila local permanece intacta para o próximo início.
"""

from __future__ import annotations

import argparse
import logging
import signal
import socket
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(ROOT))

from buffer import SegmentQueue  # noqa: E402
from capture import (  # noqa: E402
    CaptureTrack, close_audio, list_devices, resolve_device,
)
from uploader import Uploader  # noqa: E402
from vad import ensure_model  # noqa: E402

# Mesmo carregador do servidor: um único lugar decide como config.yaml e
# config.local.yaml se combinam, e o cliente ganha a resolução de ${VAR}.
from server.config import load_config  # noqa: E402

log = logging.getLogger("lifelog-client")


def main() -> int:
    parser = argparse.ArgumentParser(description="Cliente de captura Lifelog")
    parser.add_argument("--server", default=None, help="URL do servidor")
    parser.add_argument(
        "--source", choices=["mic", "system", "both"], default="both",
        help="trilhas a capturar (padrão: both)",
    )
    parser.add_argument("--device-id", default=None, help="identificador desta máquina")
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.list_devices:
        list_devices()
        return 0

    cfg = load_config()
    port = cfg.get("server.port", 8000)

    server_url = args.server or f"http://127.0.0.1:{port}"
    device_id = args.device_id or f"win-{socket.gethostname().lower()}"

    client_dir = cfg.resolve_path("server.data_dir", "./data") / "client"
    queue = SegmentQueue(client_dir / "outbox.db", client_dir / "pending")
    model_path = ensure_model(ROOT / "models" / "silero_vad.onnx")

    # Só as chaves realmente presentes no YAML são repassadas; o que faltar usa
    # o default de SileroVad. Repetir os números aqui criaria uma segunda tabela
    # de defaults para divergir da primeira.
    vad_cfg = cfg.get("capture.vad", {}) or {}
    vad_params = {
        key: type_(vad_cfg[key])
        for key, type_ in (
            ("threshold", float),
            ("min_speech_ms", int),
            ("min_silence_ms", int),
            ("padding_ms", int),
            ("max_segment_ms", int),
        )
        if vad_cfg.get(key) is not None
    }
    bitrate = int(cfg.get("capture.encode.bitrate", 24000))

    paused = threading.Event()
    sources = ["mic", "system"] if args.source == "both" else [args.source]

    tracks: list[CaptureTrack] = []
    for source in sources:
        device = resolve_device(source)
        if device is None:
            log.warning("pulando a trilha '%s': nenhum dispositivo disponível", source)
            continue
        index, channels, rate = device
        tracks.append(
            CaptureTrack(
                source, index, channels, rate, queue, vad_params, model_path,
                paused=paused, bitrate=bitrate,
            )
        )

    if not tracks:
        log.error("nenhuma trilha pôde ser aberta — veja --list-devices")
        return 1

    uploader = Uploader(queue, server_url, device_id)

    stopping = threading.Event()

    def shutdown(signum, frame):  # noqa: ARG001
        if not stopping.is_set():
            stopping.set()
            print()
            log.info("encerrando…")

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    for track in tracks:
        track.start()
    uploader.start()

    log.info(
        "capturando %s | dispositivo=%s | servidor=%s",
        " + ".join(t.source for t in tracks), device_id, server_url,
    )
    log.info("Ctrl+C para parar")

    last_report = time.time()
    try:
        while not stopping.is_set():
            time.sleep(0.5)
            if time.time() - last_report >= 60:
                stats = queue.stats()
                log.info(
                    "status: %s enviados | %s na fila (%.0fs de áudio)%s",
                    uploader.sent_count, stats["pending"], stats["queued_seconds"],
                    f" | {stats['stuck']} travados" if stats["stuck"] else "",
                )
                last_report = time.time()
    finally:
        for track in tracks:
            track.stop()
        uploader.stop()
        for track in tracks:
            track.join(timeout=10)
        uploader.join(timeout=10)

        stats = queue.stats()
        captured = sum(t.segments_captured for t in tracks)
        log.info(
            "encerrado: %s segmentos capturados, %s enviados, %s na fila",
            captured, uploader.sent_count, stats["pending"],
        )
        queue.close()
        close_audio()

    # O CPython segfaulta ao descarregar a DLL do PortAudio enquanto threads
    # WASAPI ainda finalizam por dentro — depois que todo o trabalho terminou
    # e a fila já está no disco. os._exit sai sem rodar essa finalização.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
