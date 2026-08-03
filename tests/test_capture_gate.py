"""Testes do gate de reunião dentro do laço de captura.

O laço de `CaptureTrack.run` é o coração do produto: um gate que fecha
indevidamente apaga o dia inteiro sem deixar rastro.
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

SAMPLE_RATE = 16000


class GateFalso:
    def __init__(self, aberto: bool = True):
        self.em_reuniao = aberto
        self.motivo = "teste"


class StreamFalso:
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
    def __init__(self, *a, **k):
        self.recebidos: list[np.ndarray] = []

    def process(self, chunk):
        self.recebidos.append(chunk)
        return []

    def flush(self):
        return None


def _trilha(monkeypatch, *, gate=None, leituras=5, buffer_antes_s=10.0):
    stream = StreamFalso(leituras)
    audio_falso = type("A", (), {"open": lambda *a, **k: stream})()

    monkeypatch.setattr(capture, "get_audio", lambda: audio_falso)
    monkeypatch.setattr(capture, "SileroVad", VadFalso)

    trilha = capture.CaptureTrack(
        "system", 0, 1, SAMPLE_RATE, queue=None, vad_config={},
        model_path=Path("falso.onnx"), paused=threading.Event(),
        gate=gate, buffer_antes_s=buffer_antes_s,
    )
    return trilha, stream


def test_sem_gate_grava_tudo(monkeypatch):
    """Quem não ligar o gate não deve notar diferença nenhuma."""
    trilha, _ = _trilha(monkeypatch, gate=None, leituras=4)

    trilha.run()

    assert len(trilha.vad.recebidos) == 4


def test_gate_aberto_grava(monkeypatch):
    trilha, _ = _trilha(monkeypatch, gate=GateFalso(aberto=True), leituras=4)

    trilha.run()

    assert len(trilha.vad.recebidos) == 4


def test_gate_fechado_nao_grava(monkeypatch):
    """O ponto da feature: fora de reunião, nada vira arquivo."""
    trilha, _ = _trilha(monkeypatch, gate=GateFalso(aberto=False), leituras=4)

    trilha.run()

    assert trilha.vad.recebidos == []


def test_gate_fechado_continua_drenando_o_driver(monkeypatch):
    """Parar de ler estouraria o buffer do driver."""
    trilha, stream = _trilha(monkeypatch, gate=GateFalso(aberto=False), leituras=4)

    trilha.run()

    assert stream.lidas == 4


class GateQueAbre:
    """Fecha nas primeiras N leituras e abre depois — como o detector real,
    que leva um instante para perceber a reunião."""

    def __init__(self, abrir_apos: int):
        self.abrir_apos = abrir_apos
        self.lidas = 0
        self.motivo = "teste"

    @property
    def em_reuniao(self) -> bool:
        self.lidas += 1
        return self.lidas > self.abrir_apos


def test_o_audio_antes_da_reuniao_e_recuperado(monkeypatch):
    """O detector leva um instante para perceber. Sem o buffer, as primeiras
    palavras da reunião se perderiam."""
    trilha, _ = _trilha(monkeypatch, gate=GateQueAbre(abrir_apos=3), leituras=6)

    trilha.run()

    # 3 chunks ficaram no buffer e 3 chegaram com o gate aberto: o VAD deve
    # ver os 6, não só os 3 do final.
    assert len(trilha.vad.recebidos) == 6, (
        f"esperado 6 chunks (3 do buffer + 3 novos), veio {len(trilha.vad.recebidos)}"
    )


def test_o_buffer_e_esvaziado_depois_de_usado(monkeypatch):
    """Senão o mesmo áudio seria reenviado a cada chunk seguinte."""
    trilha, _ = _trilha(monkeypatch, gate=GateQueAbre(abrir_apos=2), leituras=6)

    trilha.run()

    assert len(trilha._pre_reuniao) == 0


def test_buffer_nao_cresce_sem_limite(monkeypatch):
    """Horas fora de reunião não podem encher a memória."""
    trilha, _ = _trilha(
        monkeypatch, gate=GateFalso(aberto=False), leituras=60, buffer_antes_s=1.0,
    )

    trilha.run()

    # 1s de buffer = 10 chunks de 100ms.
    assert len(trilha._pre_reuniao) <= 10
