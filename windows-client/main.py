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
import sys
import threading
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))

from buffer import SegmentQueue  # noqa: E402
from capture import CaptureTrack, list_devices, resolve_device  # noqa: E402
from uploader import Uploader  # noqa: E402
from vad import ensure_model  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("lifelog-client")


def load_config() -> dict:
    """Lê config.yaml e aplica config.local.yaml por cima, se existir."""
    with open(ROOT / "config.yaml", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}

    local = ROOT / "config.local.yaml"
    if local.exists():
        with open(local, encoding="utf-8") as fh:
            overrides = yaml.safe_load(fh) or {}

        def merge(base: dict, over: dict) -> dict:
            out = dict(base)
            for key, value in over.items():
                if isinstance(value, dict) and isinstance(out.get(key), dict):
                    out[key] = merge(out[key], value)
                else:
                    out[key] = value
            return out

        cfg = merge(cfg, overrides)
    return cfg


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
    capture_cfg = cfg.get("capture", {})
    vad_cfg = capture_cfg.get("vad", {})
    port = cfg.get("server", {}).get("port", 8000)

    server_url = args.server or f"http://127.0.0.1:{port}"
    device_id = args.device_id or f"win-{socket.gethostname().lower()}"

    client_dir = ROOT / "data" / "client"
    queue = SegmentQueue(client_dir / "outbox.db", client_dir / "pending")
    model_path = ensure_model(ROOT / "models" / "silero_vad.onnx")

    vad_params = {
        "threshold": float(vad_cfg.get("threshold", 0.5)),
        "min_speech_ms": int(vad_cfg.get("min_speech_ms", 250)),
        "min_silence_ms": int(vad_cfg.get("min_silence_ms", 700)),
        "padding_ms": int(vad_cfg.get("padding_ms", 300)),
        "max_segment_ms": int(capture_cfg.get("chunk_seconds", 30)) * 1000,
    }
    bitrate = int(capture_cfg.get("encode", {}).get("bitrate", 24000))

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

    return 0


if __name__ == "__main__":
    sys.exit(main())
