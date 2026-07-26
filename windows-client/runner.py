"""Montagem e ciclo de vida da captura.

Extraído de main.py para que a CLI e o ícone de bandeja compartilhem a mesma
lógica — a bandeja precisa iniciar, pausar e parar exatamente o que a CLI faz.
"""

from __future__ import annotations

import logging
import socket
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from audio_source import AudioSourceProbe  # noqa: E402
from buffer import SegmentQueue  # noqa: E402
from capture import CaptureTrack, close_audio, resolve_device  # noqa: E402
from uploader import Uploader  # noqa: E402
from vad import ensure_model  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.config import load_config  # noqa: E402

log = logging.getLogger("runner")


class CaptureRunner:
    """Captura em execução: trilhas + uploader + fila.

    `pause()` interrompe a gravação sem derrubar nada — o áudio lido é
    descartado enquanto pausado, e a fila pendente continua subindo.
    """

    def __init__(self, *, server_url: str | None = None, device_id: str | None = None,
                 sources: list[str] | None = None):
        cfg = load_config()

        port = cfg.get("server.port", 8000)
        self.server_url = server_url or f"http://127.0.0.1:{port}"
        self.device_id = device_id or f"win-{socket.gethostname().lower()}"

        client_dir = cfg.resolve_path("server.data_dir", "./data") / "client"
        self.queue = SegmentQueue(client_dir / "outbox.db", client_dir / "pending")
        model_path = ensure_model(ROOT / "models" / "silero_vad.onnx")

        vad_cfg = cfg.get("capture.vad", {}) or {}
        vad_params = {
            key: type_(vad_cfg[key])
            for key, type_ in (
                ("threshold", float), ("min_speech_ms", int),
                ("min_silence_ms", int), ("padding_ms", int),
                ("max_segment_ms", int),
            )
            if vad_cfg.get(key) is not None
        }
        bitrate = int(cfg.get("capture.encode.bitrate", 24000))

        self._paused = threading.Event()
        self.tracks: list[CaptureTrack] = []

        # Um probe para todas as trilhas: o cache interno evita consultar o
        # COM a cada leitura, e só a trilha de sistema o usa de fato.
        probe = AudioSourceProbe()

        for source in sources or ["mic", "system"]:
            device = resolve_device(source)
            if device is None:
                log.warning("trilha '%s' indisponível: nenhum dispositivo", source)
                continue
            index, channels, rate = device
            self.tracks.append(
                CaptureTrack(
                    source, index, channels, rate, self.queue, vad_params, model_path,
                    paused=self._paused, bitrate=bitrate, probe=probe,
                )
            )

        self.uploader = Uploader(self.queue, self.server_url, self.device_id)
        self._started = False

    # ────────────────────────────── controle ──────────────────────────────

    @property
    def available(self) -> bool:
        return bool(self.tracks)

    @property
    def is_paused(self) -> bool:
        return self._paused.is_set()

    def start(self) -> None:
        if self._started:
            return
        for track in self.tracks:
            track.start()
        self.uploader.start()
        self._started = True
        log.info(
            "capturando %s | dispositivo=%s | servidor=%s",
            " + ".join(t.source for t in self.tracks), self.device_id, self.server_url,
        )

    def pause(self) -> None:
        """Para de gravar. O uploader segue drenando o que já está na fila."""
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    def toggle_pause(self) -> bool:
        """Alterna e devolve o novo estado (True = pausado)."""
        if self.is_paused:
            self.resume()
        else:
            self.pause()
        return self.is_paused

    def stop(self, timeout: float = 10.0) -> None:
        for track in self.tracks:
            track.stop()
        self.uploader.stop()
        for track in self.tracks:
            track.join(timeout=timeout)
        self.uploader.join(timeout=timeout)
        self.queue.close()
        close_audio()  # só depois que todas as trilhas pararam
        self._started = False

    # ─────────────────────────────── status ───────────────────────────────

    def status(self) -> dict:
        stats = self.queue.stats()
        return {
            "paused": self.is_paused,
            "sources": [t.source for t in self.tracks],
            "captured": sum(t.segments_captured for t in self.tracks),
            "sent": self.uploader.sent_count,
            "pending": stats["pending"],
            "queued_seconds": stats["queued_seconds"],
            "stuck": stats["stuck"],
        }
