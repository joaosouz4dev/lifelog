"""Detecção de fala (VAD) com Silero.

Silero é uma rede neural pequena (~2 MB ONNX) treinada em 6000+ idiomas, muito
mais precisa que o WebRTC VAD clássico e ainda assim abaixo de 1 ms por janela
na CPU. É ela que separa fala de silêncio antes de qualquer coisa subir para o
servidor — o que economiza disco, banda e tempo de transcrição.

Trabalha em janelas de 512 amostras (32 ms a 16 kHz), o tamanho que o modelo
espera. Uma máquina de estados agrupa janelas contíguas de fala em segmentos.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
WINDOW_SAMPLES = 512  # exigido pelo Silero a 16 kHz
_MODEL_URL = (
    "https://github.com/snakers4/silero-vad/raw/v5.1/src/silero_vad/data/silero_vad.onnx"
)


@dataclass
class SpeechSegment:
    """Trecho contínuo de fala pronto para envio."""

    audio: np.ndarray       # float32 mono 16 kHz, em [-1, 1]
    started_offset_ms: int  # deslocamento desde o início da captura
    duration_ms: int


def ensure_model(path: Path) -> Path:
    """Baixa o modelo ONNX na primeira execução."""
    if path.exists():
        return path
    import urllib.request

    path.parent.mkdir(parents=True, exist_ok=True)
    log.info("baixando o modelo Silero VAD…")
    urllib.request.urlretrieve(_MODEL_URL, path)  # noqa: S310 — URL fixa e confiável
    log.info("modelo VAD salvo em %s", path)
    return path


class SileroVad:
    """Detector de fala com histerese e padding.

    - `threshold`: probabilidade acima da qual a janela é considerada fala.
    - `min_speech_ms`: descarta rajadas curtas (clique de mouse, tosse).
    - `min_silence_ms`: silêncio necessário para encerrar um segmento; evita
      cortar no meio de uma pausa natural da frase.
    - `padding_ms`: margem mantida antes e depois, para não decepar sílabas.
    """

    def __init__(
        self,
        model_path: Path,
        *,
        threshold: float = 0.5,
        min_speech_ms: int = 250,
        min_silence_ms: int = 700,
        padding_ms: int = 300,
        max_segment_ms: int = 30_000,
    ):
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1
        options.log_severity_level = 3
        self._session = ort.InferenceSession(
            str(model_path), sess_options=options, providers=["CPUExecutionProvider"]
        )

        self.threshold = threshold
        self.min_speech_samples = int(min_speech_ms * SAMPLE_RATE / 1000)
        self.min_silence_samples = int(min_silence_ms * SAMPLE_RATE / 1000)
        self.padding_samples = int(padding_ms * SAMPLE_RATE / 1000)
        self.max_segment_samples = int(max_segment_ms * SAMPLE_RATE / 1000)

        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._pending = np.array([], dtype=np.float32)  # sobra entre chamadas

        # Buffer circular de pré-fala: guarda o áudio imediatamente anterior ao
        # disparo, para o segmento não começar com a primeira sílaba cortada.
        self._pre_buffer: deque[np.ndarray] = deque(
            maxlen=max(1, self.padding_samples // WINDOW_SAMPLES)
        )
        self._speech: list[np.ndarray] = []
        self._in_speech = False
        self._silence_run = 0
        self._consumed_samples = 0
        self._segment_start = 0

    def _probability(self, window: np.ndarray) -> float:
        out, self._state = self._session.run(
            None,
            {
                "input": window.reshape(1, -1).astype(np.float32),
                "state": self._state,
                "sr": np.array(SAMPLE_RATE, dtype=np.int64),
            },
        )
        return float(out[0][0])

    def process(self, audio: np.ndarray) -> list[SpeechSegment]:
        """Consome áudio e devolve os segmentos de fala já finalizados."""
        self._pending = np.concatenate([self._pending, audio.astype(np.float32)])
        segments: list[SpeechSegment] = []

        while len(self._pending) >= WINDOW_SAMPLES:
            window = self._pending[:WINDOW_SAMPLES]
            self._pending = self._pending[WINDOW_SAMPLES:]
            is_speech = self._probability(window) >= self.threshold

            if is_speech:
                if not self._in_speech:
                    # Começou a falar: recupera o padding anterior.
                    self._in_speech = True
                    self._speech = list(self._pre_buffer)
                    padding = len(self._speech) * WINDOW_SAMPLES
                    self._segment_start = max(0, self._consumed_samples - padding)
                    self._pre_buffer.clear()
                self._speech.append(window)
                self._silence_run = 0
            elif self._in_speech:
                # Silêncio dentro da fala: mantém no buffer (vira padding final)
                # até atingir o limite que encerra o segmento.
                self._speech.append(window)
                self._silence_run += WINDOW_SAMPLES
                if self._silence_run >= self.min_silence_samples:
                    segment = self._close_segment()
                    if segment is not None:
                        segments.append(segment)
            else:
                self._pre_buffer.append(window)

            self._consumed_samples += WINDOW_SAMPLES

            # Fala longa sem pausa: corta para o segmento não crescer sem limite.
            if self._in_speech and len(self._speech) * WINDOW_SAMPLES >= self.max_segment_samples:
                segment = self._close_segment(force=True)
                if segment is not None:
                    segments.append(segment)

        return segments

    def _close_segment(self, *, force: bool = False) -> SpeechSegment | None:
        """Fecha o segmento em curso, aparando o silêncio excedente."""
        if not self._speech:
            self._in_speech = False
            return None

        audio = np.concatenate(self._speech)

        if not force and self._silence_run > self.padding_samples:
            # Mantém só `padding_ms` de silêncio no fim; o resto é descartado.
            trim = self._silence_run - self.padding_samples
            if trim < len(audio):
                audio = audio[:-trim]

        self._speech = []
        self._in_speech = False
        self._silence_run = 0
        self._pre_buffer.clear()

        if len(audio) < self.min_speech_samples:
            return None  # rajada curta demais para ser fala

        return SpeechSegment(
            audio=audio,
            started_offset_ms=int(self._segment_start * 1000 / SAMPLE_RATE),
            duration_ms=int(len(audio) * 1000 / SAMPLE_RATE),
        )

    def flush(self) -> SpeechSegment | None:
        """Encerra o segmento pendente — chamar ao parar a captura."""
        if self._in_speech:
            return self._close_segment(force=True)
        return None
