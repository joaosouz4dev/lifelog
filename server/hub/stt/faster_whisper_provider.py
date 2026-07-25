"""Transcrição local com faster-whisper (CTranslate2).

Custo zero, sem rede, sem áudio saindo da máquina. É o provedor primário.
O modelo é carregado sob demanda e reaproveitado; a transcrição roda numa
thread separada para não travar o event loop.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from ...models import Transcript
from ..base import ProviderError

log = logging.getLogger(__name__)


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
        self._model = None
        self._load_lock = asyncio.Lock()
        self._load_error: str | None = None

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
        return self._model

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
