"""Regressão do laço de captura depois da reorganização para o ditado.

O laço de `CaptureTrack.run` é o coração do produto: se ele quebrar, o
Lifelog para de gravar. A leitura do stream foi unificada e o tap entrou
antes da checagem de pausa — estes testes provam que nada mais mudou.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import numpy as np
import pytest

CLIENTE = Path(__file__).resolve().parent.parent / "windows-client"
if str(CLIENTE) not in sys.path:
    sys.path.insert(0, str(CLIENTE))

pytest.importorskip("pyaudiowpatch", reason="a captura só roda no Windows")

import capture  # noqa: E402
from dictation import DictationTap  # noqa: E402

SAMPLE_RATE = 16000


class StreamFalso:
    """Devolve blocos de áudio e para depois de N leituras."""

    def __init__(self, leituras: int):
        self.restantes = leituras
        self.lidas = 0

    def read(self, quadros, exception_on_overflow=False):
        if self.restantes <= 0:
            raise _Fim()
        self.restantes -= 1
        self.lidas += 1
        return np.full(quadros, 0.1, dtype=np.float32).tobytes()

    def stop_stream(self):
        pass

    def close(self):
        pass


class _Fim(Exception):
    """Encerra o laço sem simular falha de driver."""


class VadFalso:
    """Registra o que chegou; nunca devolve segmento."""

    def __init__(self, *a, **k):
        self.recebidos: list[np.ndarray] = []

    def process(self, chunk):
        self.recebidos.append(chunk)
        return []

    def flush(self):
        return None


def _trilha(monkeypatch, *, tap=None, leituras=5, pausado=False):
    stream = StreamFalso(leituras)
    audio_falso = type("A", (), {"open": lambda *a, **k: stream})()

    monkeypatch.setattr(capture, "get_audio", lambda: audio_falso)
    monkeypatch.setattr(capture, "SileroVad", VadFalso)

    pausa = threading.Event()
    if pausado:
        pausa.set()

    trilha = capture.CaptureTrack(
        "mic", 0, 1, SAMPLE_RATE, queue=None, vad_config={},
        model_path=Path("falso.onnx"), paused=pausa, tap=tap,
    )
    return trilha, stream


def test_o_audio_chega_ao_vad_quando_nao_ha_ditado(monkeypatch):
    """O caminho normal precisa continuar igual."""
    trilha, stream = _trilha(monkeypatch, leituras=3)

    trilha.run()

    assert stream.lidas == 3
    assert len(trilha.vad.recebidos) == 3


def test_o_tap_recebe_o_audio_e_o_vad_nao(monkeypatch):
    """Deixar o chunk seguir faria o ditado ser transcrito duas vezes."""
    tap = DictationTap()
    tap.comecar()
    trilha, _ = _trilha(monkeypatch, tap=tap, leituras=3)

    trilha.run()

    assert trilha.vad.recebidos == [], "o VAD não pode ver o áudio do ditado"
    audio = tap.terminar()
    assert audio is not None and audio.size > 0


def test_o_tap_inativo_nao_interfere(monkeypatch):
    """Sem ditado em curso, tudo segue para o VAD."""
    tap = DictationTap()
    trilha, _ = _trilha(monkeypatch, tap=tap, leituras=3)

    trilha.run()

    assert len(trilha.vad.recebidos) == 3


def test_o_ditado_funciona_com_a_captura_pausada(monkeypatch):
    """Pausar o lifelog é justamente quando mais se quer ditar."""
    tap = DictationTap()
    tap.comecar()
    trilha, _ = _trilha(monkeypatch, tap=tap, leituras=3, pausado=True)

    trilha.run()

    audio = tap.terminar()
    assert audio is not None, "o ditado deveria gravar mesmo pausado"


def test_pausado_continua_drenando_o_driver(monkeypatch):
    """Parar de ler estouraria o buffer do driver."""
    trilha, stream = _trilha(monkeypatch, leituras=4, pausado=True)

    trilha.run()

    assert stream.lidas == 4, "deveria ler mesmo pausado"
    assert trilha.vad.recebidos == [], "mas descartar tudo"
