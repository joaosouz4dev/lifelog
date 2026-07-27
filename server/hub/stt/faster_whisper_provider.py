"""Transcrição local com faster-whisper (CTranslate2).

Custo zero, sem rede, sem áudio saindo da máquina. É o provedor primário.
O modelo é carregado sob demanda e reaproveitado; a transcrição roda numa
thread separada para não travar o event loop.

Depois de um tempo sem transcrever, o modelo é descarregado: o `large-v3`
ocupa ~5 GB da VRAM, o que numa placa de 8 GB não sobra para mais nada.
Recarregar custa ~7 s, pago só na primeira fala depois do intervalo ocioso.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import time
from pathlib import Path
from typing import Any

from ...models import Transcript
from ..base import ProviderError

log = logging.getLogger(__name__)

# Curto demais recarrega no meio de uma conversa com pausas; longo demais
# segura a VRAM à toa. Cinco minutos cobre a pausa entre frases sem prender
# a placa enquanto ninguém fala.
IDLE_UNLOAD_SECONDS = 300


class FasterWhisperProvider:
    name: str
    requires_network = False

    def __init__(self, name: str, cfg: dict[str, Any]):
        self.name = name
        self.model_size = cfg.get("model", "large-v3")
        self.device = cfg.get("device", "cuda")
        self.compute_type = cfg.get("compute_type", "float16")
        self.default_language = cfg.get("language", "pt")
        self.beam_size = int(cfg.get("beam_size", 5))
        # 0 desativa a descarga — útil em máquina com VRAM sobrando, onde a
        # recarga só atrapalharia.
        self.idle_unload_seconds = float(
            cfg.get("idle_unload_seconds", IDLE_UNLOAD_SECONDS)
        )
        self._model = None
        self._load_lock = asyncio.Lock()
        self._load_error: str | None = None
        self._last_used = 0.0
        self._idle_task: asyncio.Task | None = None

    # ────────────────────────────── modelo ──────────────────────────────

    def _load_model_sync(self):
        from faster_whisper import WhisperModel

        device, compute_type = self.device, self.compute_type
        try:
            return WhisperModel(self.model_size, device=device, compute_type=compute_type)
        except Exception as exc:
            if device != "cpu":
                # Sem GPU utilizável (driver, VRAM, CUDA ausente) — cai para CPU
                # em int8 em vez de deixar o provedor primário indisponível.
                log.warning(
                    "%s: falha ao carregar em %s/%s (%s); caindo para cpu/int8",
                    self.name, device, compute_type, exc,
                )
                self.device, self.compute_type = "cpu", "int8"
                return WhisperModel(self.model_size, device="cpu", compute_type="int8")
            raise

    async def _get_model(self):
        if self._model is not None:
            return self._model
        async with self._load_lock:
            if self._model is None:
                log.info(
                    "carregando %s (%s, %s)…", self.model_size, self.device, self.compute_type
                )
                try:
                    self._model = await asyncio.to_thread(self._load_model_sync)
                except Exception as exc:
                    self._load_error = repr(exc)
                    raise ProviderError(
                        f"não foi possível carregar o modelo: {exc}",
                        provider=self.name,
                    ) from exc
                log.info("modelo pronto (%s, %s)", self.device, self.compute_type)
        self._last_used = time.monotonic()
        self._ensure_idle_watcher()
        return self._model

    def _ensure_idle_watcher(self) -> None:
        """Sobe o vigia de ociosidade, se ainda não estiver rodando."""
        if self.idle_unload_seconds <= 0:
            return
        if self._idle_task is None or self._idle_task.done():
            self._idle_task = asyncio.create_task(self._watch_idle())

    async def _watch_idle(self) -> None:
        """Descarrega o modelo depois de um tempo sem transcrever."""
        try:
            while True:
                await asyncio.sleep(min(30.0, self.idle_unload_seconds))
                if self._model is None:
                    return
                ocioso = time.monotonic() - self._last_used
                if ocioso >= self.idle_unload_seconds:
                    await self.unload()
                    return
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - o vigia não pode derrubar o app
            log.exception("%s: falha no vigia de ociosidade", self.name)

    async def unload(self) -> None:
        """Libera o modelo e a VRAM que ele segura."""
        async with self._load_lock:
            if self._model is None:
                return
            self._model = None
            # CTranslate2 devolve a VRAM ao coletar o objeto, não ao perder a
            # referência — sem o gc.collect() a placa continua ocupada.
            await asyncio.to_thread(gc.collect)
            log.info(
                "%s: modelo descarregado após %.0fs sem uso",
                self.name, self.idle_unload_seconds,
            )

    # ─────────────────────────── transcrição ───────────────────────────

    def _transcribe_sync(self, model, audio: Path, language: str) -> Transcript:
        segments, info = model.transcribe(
            str(audio),
            language=language,
            beam_size=self.beam_size,
            vad_filter=False,  # o cliente já aplicou VAD antes de enviar
        )

        parts: list[str] = []
        logprobs: list[float] = []
        for seg in segments:  # gerador preguiçoso: só aqui o trabalho acontece
            parts.append(seg.text)
            if seg.avg_logprob is not None:
                logprobs.append(seg.avg_logprob)

        text = " ".join(p.strip() for p in parts if p.strip()).strip()

        # avg_logprob é negativo (log de probabilidade). exp() traz para 0..1,
        # o que dá uma confiança comparável entre provedores.
        confidence: float | None = None
        if logprobs:
            import math

            confidence = round(min(1.0, math.exp(sum(logprobs) / len(logprobs))), 4)

        return Transcript(
            text=text,
            language=getattr(info, "language", language),
            confidence=confidence,
            provider=self.name,
            cost_cents=0.0,
        )

    async def transcribe(self, audio: Path, language: str = "pt") -> Transcript:
        if not audio.exists():
            raise ProviderError(
                f"arquivo não encontrado: {audio}", provider=self.name, retryable=False
            )
        model = await self._get_model()
        try:
            return await asyncio.to_thread(
                self._transcribe_sync, model, audio, language or self.default_language
            )
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(f"falha na transcrição: {exc}", provider=self.name) from exc
        finally:
            # Marcado no fim, não no início: uma transcrição longa não pode
            # deixar o vigia achar que o modelo está ocioso enquanto trabalha.
            self._last_used = time.monotonic()

    # ──────────────────────────── diagnóstico ───────────────────────────

    async def health(self) -> bool:
        if self._model is not None:
            return True
        try:
            from importlib.util import find_spec

            return find_spec("faster_whisper") is not None
        except Exception:
            return False

    def estimate_cost_cents(self, duration_ms: int) -> float:
        return 0.0  # roda na sua máquina
