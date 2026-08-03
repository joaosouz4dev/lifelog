"""Captura de áudio no Windows.

Grava duas trilhas em paralelo — microfone e áudio do sistema (WASAPI loopback,
que pega tudo que sai da placa de som, sem as restrições do Android) — aplica
VAD em cada uma, e envia só os trechos com fala para o servidor.

A CLI vive em main.py; este módulo só expõe as peças de captura.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))

from vad import SAMPLE_RATE, SileroVad  # noqa: E402

if TYPE_CHECKING:
    from buffer import SegmentQueue

log = logging.getLogger("capture")

# ────────────────────────── PortAudio compartilhado ──────────────────────────
# Uma instância por processo, criada sob lock. Instanciar PyAudio() dentro de
# cada thread de captura derruba o processo com segfault assim que duas trilhas
# leem ao mesmo tempo — reproduzido de forma isolada com dois PyAudio() e dois
# streams WASAPI, sem nenhum código deste projeto envolvido.
_audio = None
_audio_lock = threading.Lock()


def get_audio():
    """Devolve a instância única do PortAudio, criando-a na primeira chamada."""
    global _audio
    with _audio_lock:
        if _audio is None:
            import pyaudiowpatch as pyaudio

            _audio = pyaudio.PyAudio()
        return _audio


def close_audio() -> None:
    """Encerra o PortAudio. Chamar só depois que todas as trilhas pararam.

    Um `terminate()` disparado imediatamente após fechar streams WASAPI ainda
    pega o driver liberando recursos e derruba o processo. A pausa curta dá
    esse tempo; o try/except cobre o resto, já que aqui o trabalho todo já
    terminou e um erro de encerramento não deve virar crash.
    """
    global _audio
    with _audio_lock:
        if _audio is None:
            return
        time.sleep(0.3)
        try:
            _audio.terminate()
        except Exception:
            log.debug("falha ao encerrar o PortAudio", exc_info=True)
        _audio = None


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
        # Sem isto o Windows abre um console para cada chamada. Como há uma
        # por segmento de fala, a tela pisca a cada frase capturada.
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
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
        probe=None,
        tap=None,
        gate=None,
        buffer_antes_s: float = 10.0,
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
        # Descobre qual app está tocando, para o relatório poder separar
        # reunião de série depois. None desliga a marcação.
        self.probe = probe
        # Desvia o áudio para o ditado quando ele está gravando. Só a trilha
        # do microfone recebe um: ditado é a voz de quem fala, e alimentar
        # com o loopback misturaria o som do sistema na frase.
        self.tap = tap
        # Decide se há reunião em curso. None mantém o comportamento antigo:
        # grava sempre.
        self.gate = gate
        # Últimos segundos antes de o gate abrir. Os chunks chegam de 100ms,
        # então o deque guarda a janela inteira sem crescer sem limite.
        self._pre_reuniao: deque[np.ndarray] = deque(maxlen=int(buffer_antes_s * 10))
        self._fora_de_reuniao = False
        # `_stopping`, não `_stop`: threading.Thread já tem um método _stop()
        # interno, e sobrescrevê-lo com um Event faz join() estourar TypeError.
        self._stopping = threading.Event()
        self.segments_captured = 0

    def stop(self) -> None:
        self._stopping.set()

    def run(self) -> None:
        import pyaudiowpatch as pyaudio

        # A instância do PortAudio vem de fora e é compartilhada: criar uma
        # PyAudio() por thread causa segfault no WASAPI quando duas trilhas
        # rodam juntas (mic + system, que é o modo padrão). Ver get_audio().
        audio = get_audio()
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

            while not self._stopping.is_set():
                # A leitura é única e vem antes de tudo: o driver precisa ser
                # drenado mesmo pausado, senão o buffer estoura. O que muda
                # entre pausado e ativo é o que se faz com o áudio, não se
                # ele é lido.
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

                # O ditado vem antes da pausa de propósito: pausar o lifelog
                # não deveria desligar o ditado — é justamente quando mais se
                # quer ditar sem registrar.
                if self.tap is not None and self.tap.ativo:
                    self.tap.alimentar(chunk)
                    # E o chunk para aqui: deixá-lo seguir faria o VAD capturar
                    # a mesma fala, transcrevê-la uma segunda vez e colocá-la
                    # na timeline — um ditado é comando, não registro de vida.
                    continue

                if self.paused.is_set():
                    if not was_paused:
                        log.info("[%s] pausado", self.source)
                        self._flush()
                        was_paused = True
                    continue

                if was_paused:
                    log.info("[%s] retomado", self.source)
                    was_paused = False
                    stream_start = datetime.now()

                # Gate de reunião: fora de uma chamada nada é gravado. Fica
                # antes do VAD de propósito — cortar aqui evita a inferência
                # do Silero a cada 100 ms, o ffmpeg, a escrita em disco e o
                # upload. É o mesmo lugar onde a pausa já opera.
                if self.gate is not None and not self.gate.em_reuniao:
                    # Guarda os últimos segundos: quando a reunião começa, o
                    # detector leva um instante para perceber, e sem isto as
                    # primeiras palavras se perderiam.
                    self._pre_reuniao.append(chunk)
                    if not self._fora_de_reuniao:
                        log.info("[%s] fora de reunião — captura em espera", self.source)
                        self._flush()
                        self._fora_de_reuniao = True
                    continue

                if self._fora_de_reuniao:
                    log.info("[%s] reunião detectada — gravando", self.source)
                    self._fora_de_reuniao = False
                    stream_start = datetime.now()
                    # O áudio que antecedeu a detecção entra primeiro, na
                    # ordem em que foi falado.
                    for anterior in self._pre_reuniao:
                        for segment in self.vad.process(anterior):
                            self._emit(segment, stream_start)
                self._pre_reuniao.clear()

                for segment in self.vad.process(chunk):
                    self._emit(segment, stream_start)

        except Exception:
            log.exception("[%s] captura interrompida", self.source)
        finally:
            self._flush()
            if stream is not None:
                stream.stop_stream()
                stream.close()
            # Não chamar audio.terminate(): a instância é compartilhada com as
            # outras trilhas. Quem encerra é close_audio(), no fim do processo.
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

        # Só o áudio do sistema tem um app de origem — o microfone é a pessoa.
        # É esse rótulo que permite ao relatório ignorar série e música depois.
        app_name = self.probe.snapshot() if (self.probe and self.source == "system") else None

        started_at = stream_start + timedelta(milliseconds=segment.started_offset_ms)
        self.queue.enqueue(
            payload,
            source=self.source,
            started_at=started_at,
            duration_ms=segment.duration_ms,
            app_name=app_name,
        )
        self.segments_captured += 1
        log.info(
            "[%s] fala de %.1fs enfileirada (%.1f KB)",
            self.source, segment.duration_ms / 1000, len(payload) / 1024,
        )


# ──────────────────────────── dispositivos ────────────────────────────


def list_devices() -> None:
    audio = get_audio()
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
        # Só encerra aqui porque --list-devices é um comando de diagnóstico que
        # sai logo em seguida. No fluxo de captura quem encerra é o main.
        close_audio()


def resolve_device(source: str):
    """Descobre índice, canais e taxa do dispositivo da trilha."""
    audio = get_audio()
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
    # Sem terminate(): esta função roda antes de abrir as trilhas, e encerrar
    # aqui deixaria a instância compartilhada morta quando elas subissem.
