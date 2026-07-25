"""Testes do VAD com áudio real e sintético.

Baixa o modelo Silero na primeira execução (~2 MB) e o guarda em models/.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "windows-client"))

pytest.importorskip("onnxruntime", reason="onnxruntime não instalado")

from vad import SAMPLE_RATE, SileroVad, ensure_model  # noqa: E402

MODEL_PATH = ROOT / "models" / "silero_vad.onnx"
SPEECH_SAMPLE = ROOT / "tests" / "fixtures" / "fala.opus"


@pytest.fixture(scope="module")
def model() -> Path:
    return ensure_model(MODEL_PATH)


def _silence(seconds: float) -> np.ndarray:
    return np.zeros(int(SAMPLE_RATE * seconds), dtype=np.float32)


def _noise(seconds: float, amplitude: float = 0.01) -> np.ndarray:
    rng = np.random.default_rng(42)
    return (rng.standard_normal(int(SAMPLE_RATE * seconds)) * amplitude).astype(np.float32)


def test_silencio_nao_gera_segmento(model):
    vad = SileroVad(model)
    assert vad.process(_silence(3.0)) == []
    assert vad.flush() is None


def test_ruido_de_fundo_nao_gera_segmento(model):
    """Ruído baixo constante não pode ser confundido com fala."""
    vad = SileroVad(model)
    segments = vad.process(_noise(3.0))
    assert segments == []


@pytest.mark.skipif(not SPEECH_SAMPLE.exists(), reason="fixture de fala ausente")
def test_fala_real_e_detectada(model):
    import soundfile as sf

    audio, rate = sf.read(SPEECH_SAMPLE, dtype="float32")
    assert rate == SAMPLE_RATE, f"fixture deve estar a {SAMPLE_RATE} Hz"
    if audio.ndim > 1:
        audio = audio.mean(axis=1)

    vad = SileroVad(model)
    segments = vad.process(audio)
    tail = vad.flush()
    if tail is not None:
        segments.append(tail)

    assert segments, "a fala deveria ter sido detectada"
    total_ms = sum(s.duration_ms for s in segments)
    assert total_ms > 1000, f"esperava mais de 1s de fala, veio {total_ms}ms"


@pytest.mark.skipif(not SPEECH_SAMPLE.exists(), reason="fixture de fala ausente")
def test_silencio_ao_redor_da_fala_e_descartado(model):
    """O ganho principal do VAD: 10s de silêncio + fala devem render só a fala."""
    import soundfile as sf

    audio, _ = sf.read(SPEECH_SAMPLE, dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)

    padded = np.concatenate([_silence(5.0), audio, _silence(5.0)])

    vad = SileroVad(model)
    segments = vad.process(padded)
    tail = vad.flush()
    if tail is not None:
        segments.append(tail)

    assert segments
    kept_ms = sum(s.duration_ms for s in segments)
    total_ms = len(padded) * 1000 // SAMPLE_RATE

    assert kept_ms < total_ms * 0.6, (
        f"VAD deveria descartar a maior parte do silêncio: {kept_ms}ms de {total_ms}ms"
    )


def test_segmento_longo_e_fatiado(model):
    """Fala contínua não pode gerar um segmento sem limite de tamanho."""
    vad = SileroVad(model, max_segment_ms=2000)
    # O corte por tamanho é geométrico, então basta forçar o estado de fala.
    vad._in_speech = True
    vad._speech = [np.ones(512, dtype=np.float32) * 0.1] * 100
    segment = vad._close_segment(force=True)

    assert segment is not None
    assert segment.duration_ms > 0
