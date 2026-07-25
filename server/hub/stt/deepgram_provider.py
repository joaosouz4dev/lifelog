"""Transcrição via Deepgram — fallback quando o local não está disponível.

Usado quando a máquina do servidor não tem GPU no momento, o modelo falha ao
carregar, ou o servidor roda em outro lugar. Cobrado por minuto de áudio.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx

from ...models import Transcript
from ..base import ProviderError

log = logging.getLogger(__name__)

_API_URL = "https://api.deepgram.com/v1/listen"

# Erros que não melhoram com nova tentativa nem em outro provedor.
_FATAL_STATUS = {400, 401, 403, 413}


class DeepgramProvider:
    name: str
    requires_network = True

    def __init__(self, name: str, cfg: dict[str, Any]):
        self.name = name
        self.api_key = cfg.get("api_key")
        self.model = cfg.get("model", "nova-2")
        self.language = cfg.get("language", "pt-BR")
        # ~US$0,0043/min ≈ 0,43 centavos de dólar por minuto.
        self.cost_per_minute_cents = float(cfg.get("cost_per_minute_cents", 0.43))
        self.timeout = float(cfg.get("timeout_seconds", 120))

    async def transcribe(self, audio: Path, language: str = "pt") -> Transcript:
        if not self.api_key:
            raise ProviderError("api_key ausente", provider=self.name, retryable=False)
        if not audio.exists():
            raise ProviderError(
                f"arquivo não encontrado: {audio}", provider=self.name, retryable=False
            )

        params = {
            "model": self.model,
            "language": self.language,
            "punctuate": "true",
            "smart_format": "true",
        }
        headers = {
            "Authorization": f"Token {self.api_key}",
            "Content-Type": "audio/ogg",  # container Opus
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    _API_URL, params=params, headers=headers, content=audio.read_bytes()
                )
        except httpx.HTTPError as exc:
            raise ProviderError(f"erro de rede: {exc}", provider=self.name) from exc

        if response.status_code != 200:
            body = response.text[:300]
            raise ProviderError(
                f"HTTP {response.status_code}: {body}",
                provider=self.name,
                retryable=response.status_code not in _FATAL_STATUS,
            )

        try:
            alt = response.json()["results"]["channels"][0]["alternatives"][0]
        except (KeyError, IndexError, ValueError) as exc:
            raise ProviderError(
                f"resposta inesperada: {exc}", provider=self.name
            ) from exc

        duration_min = self._duration_minutes(response.json())
        return Transcript(
            text=(alt.get("transcript") or "").strip(),
            language=self.language,
            confidence=alt.get("confidence"),
            provider=self.name,
            cost_cents=round(duration_min * self.cost_per_minute_cents, 4),
        )

    @staticmethod
    def _duration_minutes(payload: dict) -> float:
        seconds = payload.get("metadata", {}).get("duration", 0) or 0
        return float(seconds) / 60.0

    async def health(self) -> bool:
        return bool(self.api_key)

    def estimate_cost_cents(self, duration_ms: int) -> float:
        return (duration_ms / 60_000.0) * self.cost_per_minute_cents
