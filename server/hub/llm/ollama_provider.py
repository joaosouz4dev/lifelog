"""Geração de texto via Ollama local.

Custo zero e sem rede externa. Útil como fallback quando o teto de gasto
estoura, ou para quem prefere não mandar transcrição alguma para fora.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ...models import Completion
from ..base import ProviderError

log = logging.getLogger(__name__)


class OllamaProvider:
    name: str
    requires_network = False  # localhost

    def __init__(self, name: str, cfg: dict[str, Any]):
        self.name = name
        self.base_url = str(cfg.get("base_url", "http://localhost:11434")).rstrip("/")
        self.model = cfg.get("model", "qwen2.5:14b")
        # Modelo local em CPU é lento; um relatório diário pode levar minutos.
        self.timeout = float(cfg.get("timeout_seconds", 600))

    async def complete(
        self, prompt: str, *, system: str | None = None, max_tokens: int | None = None
    ) -> Completion:
        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
        }
        if system:
            payload["system"] = system
        if max_tokens:
            payload["options"] = {"num_predict": max_tokens}

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(f"{self.base_url}/api/generate", json=payload)
        except httpx.HTTPError as exc:
            raise ProviderError(f"erro de rede: {exc}", provider=self.name) from exc

        if response.status_code != 200:
            raise ProviderError(
                f"HTTP {response.status_code}: {response.text[:200]}",
                provider=self.name,
                # 404 aqui significa modelo não baixado — `ollama pull` resolve,
                # repetir não.
                retryable=response.status_code != 404,
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError(f"resposta inválida: {exc}", provider=self.name) from exc

        return Completion(
            text=(data.get("response") or "").strip(),
            provider=self.name,
            model=self.model,
            tokens_in=data.get("prompt_eval_count", 0),
            tokens_out=data.get("eval_count", 0),
            cost_cents=0.0,
            stop_reason=data.get("done_reason"),
        )

    async def health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                return (await client.get(f"{self.base_url}/api/tags")).status_code == 200
        except Exception:
            return False

    def estimate_cost_cents(self, tokens_in: int, tokens_out: int) -> float:
        return 0.0  # roda na sua máquina
