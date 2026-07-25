"""Cliente de captura para Windows.

Grava duas trilhas em paralelo — microfone e áudio do sistema (WASAPI loopback,
que pega tudo que sai da placa de som, sem as restrições do Android) — aplica
VAD em cada uma, e envia só os trechos com fala para o servidor.

Uso:
    python capture.py                      # ambas as trilhas
    python capture.py --source mic         # só microfone
    python capture.py --list-devices       # diagnóstico
"""

from __future__ import annotations

import argparse
import io
import logging
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))

from buffer import SegmentQueue  # noqa: E402
from vad import SAMPLE_RATE, SileroVad, ensure_model  # noqa: E402

log = logging.getLogger("capture")

ROOT = Path(__file__).resolve().parent.parent


# ─────────────────────────────── áudio ───────────────────────────────


def to_mono_16k(chunk: np.ndarray, channels: int, source_rate: int) -> np.ndarray:
    """Converte para mono 16 kHz float32, o formato que o VAD e o Whisper esperam."""
    if channels > 1:
        chunk = chunk.reshape(-1, channels).mean(axis=1)

    if source_rate != SAMPLE_RATE:
        # Reamostragem linear: barata e suficiente para voz, que vive bem
        # abaixo da Nyquist de 8 kHz.
        target_len = int(len(chunk) * SAMPLE_RATE / source_rate)
        if target_len == 0:
            return np.array([], dtype=np.float32)
        positions = np.linspace(0, len(chunk) - 1, target_len)
        chunk = np.interp(positions, np.arange(len(chunk)), chunk)

    return chunk.astype(np.float32)


def encode_opus(audio: np.ndarray, bitrate: int = 24000) -> bytes:
    """Codifica em Opus via ffmpeg. ~24 kbps é transparente para voz."""
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
    process = subprocess.run(
        [
            "ffmpeg", "-loglevel", "error",
            "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1", "-i", "pipe:0",
            "-c:a", "libopus", "-b:a", str(bitrate), "-f", "ogg", "pipe:1",
        ],
        input=pcm, capture_output=True, check=False,
    )
    if process.returncode != 0:
        raise RuntimeError(f"ffmpeg falhou: {process.stderr.decode()[:300]}")
    return process.stdout


# ──────────────────────────── trilha de captura ────────────────────────────


class CaptureTrack(threading.Thread):
    """Uma trilha: abre o dispositivo, aplica VAD e enfileira os segmentos."""

    def __init__(
        self,
        source: str,
        device_index: int,
        channels: int,
        source_rate: int,
        queue: SegmentQueue,
        vad_config: dict,
        model_path: Path,
        *,
        paused: threading.Event,
        bitrate: int = 24000,
    ):
        super().__init__(name=f"capture-{source}", daemon=True)
        self.source = source
        self.device_index = device_index
        self.channels = channels
        self.source_rate = source_rate
        self.queue = queue
        self.vad = SileroVad(model_path, **vad_config)
        self.bitrate = bitrate
        self.paused = paused
        self._stop = threading.Event()
        self.segments_captured = 0

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        import pyaudiowpatch as pyaudio

        audio = pyaudio.PyAudio()
        stream = None
        try:
            stream = audio.open(
                format=pyaudio.paFloat32,
                channels=self.channels,
                rate=self.source_rate,
                input=True,
                input_device_index=self.device_index,
                frames_per_buffer=int(self.source_rate * 0.1),  # 100 ms
            )
            log.info("[%s] captura iniciada (%s Hz, %s canais)",
                     self.source, self.source_rate, self.channels)

            stream_start = datetime.now()
            was_paused = False

            while not self._stop.is_set():
                if self.paused.is_set():
                    if not was_paused:
                        log.info("[%s] pausado", self.source)
                        self._flush()
                        was_paused = True
                    # Continua lendo para não estourar o buffer do driver,
                    # mas descarta tudo — nada é gravado enquanto pausado.
                    stream.read(int(self.source_rate * 0.1), exception_on_overflow=False)
                    continue

                if was_paused:
                    log.info("[%s] retomado", self.source)
                    was_paused = False
                    stream_start = datetime.now()

                try:
                    raw = stream.read(
                        int(self.source_rate * 0.1), exception_on_overflow=False
                    )
                except OSError as exc:
                    log.warning("[%s] falha na leitura: %s", self.source, exc)
                    time.sleep(0.5)
                    continue

                chunk = to_mono_16k(
                    np.frombuffer(raw, dtype=np.float32), self.channels, self.source_rate
                )
                if chunk.size == 0:
                    continue

                for segment in self.vad.process(chunk):
                    self._emit(segment, stream_start)

        except Exception:
            log.exception("[%s] captura interrompida", self.source)
        finally:
            self._flush()
            if stream is not None:
                stream.stop_stream()
                stream.close()
            audio.terminate()
            log.info("[%s] finalizada (%s segmentos)", self.source, self.segments_captured)

    def _flush(self) -> None:
        tail = self.vad.flush()
        if tail is not None:
            self._emit(tail, datetime.now() - timedelta(milliseconds=tail.duration_ms))

    def _emit(self, segment, stream_start: datetime) -> None:
        try:
            payload = encode_opus(segment.audio, self.bitrate)
        except Exception:
            log.exception("[%s] falha ao codificar segmento", self.source)
            return

        started_at = stream_start + timedelta(milliseconds=segment.started_offset_ms)
        self.queue.enqueue(
            payload,
            source=self.source,
            started_at=started_at,
            duration_ms=segment.duration_ms,
        )
        self.segments_captured += 1
        log.info(
            "[%s] fala de %.1fs enfileirada (%.1f KB)",
            self.source, segment.duration_ms / 1000, len(payload) / 1024,
        )


# ──────────────────────────── dispositivos ────────────────────────────


def list_devices() -> None:
    import pyaudiowpatch as pyaudio

    audio = pyaudio.PyAudio()
    try:
        print("\n=== Dispositivos WASAPI ===\n")
        try:
            loop = audio.get_default_wasapi_loopback()
            print(f"Loopback padrão  : [{loop['index']}] {loop['name']}")
            print(f"                   {int(loop['defaultSampleRate'])} Hz, "
                  f"{loop['maxInputChannels']} canais")
        except Exception as exc:
            print(f"Loopback padrão  : indisponível ({exc})")

        try:
            mic = audio.get_default_input_device_info()
            print(f"\nMicrofone padrão : [{mic['index']}] {mic['name']}")
            print(f"                   {int(mic['defaultSampleRate'])} Hz, "
                  f"{mic['maxInputChannels']} canais")
        except Exception as exc:
            print(f"\nMicrofone padrão : indisponível ({exc})")

        print("\n--- Todas as entradas ---")
        for i in range(audio.get_device_count()):
            info = audio.get_device_info_by_index(i)
            if info["maxInputChannels"] > 0:
                tag = " [loopback]" if info.get("isLoopbackDevice") else ""
                print(f"  [{i}] {info['name']}{tag}")
    finally:
        audio.terminate()


def resolve_device(source: str):
    """Descobre índice, canais e taxa do dispositivo da trilha."""
    import pyaudiowpatch as pyaudio

    audio = pyaudio.PyAudio()
    try:
        info = (
            audio.get_default_wasapi_loopback()
            if source == "system"
            else audio.get_default_input_device_info()
        )
        if info is None:
            return None
        return int(info["index"]), int(info["maxInputChannels"]), int(info["defaultSampleRate"])
    except Exception as exc:
        log.error("dispositivo para '%s' indisponível: %s", source, exc)
        return None
    finally:
        audio.terminate()
