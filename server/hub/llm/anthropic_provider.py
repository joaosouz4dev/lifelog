"""Geração de texto via Claude (API Anthropic).

Usado para os relatórios diário e mensal, e para o chat sobre as transcrições.
"""

from __future__ import annotations

import logging
from typing import Any

from ...models import Completion
from ..base import ProviderError

log = logging.getLogger(__name__)

# Preço por milhão de tokens, em centavos de dólar. Serve para o teto de gasto
# do hub — confira em platform.claude.com/docs/en/pricing se mudar.
_PRICING = {
    "claude-opus-5": (500.0, 2500.0),
    "claude-sonnet-5": (300.0, 1500.0),
    "claude-haiku-4-5": (100.0, 500.0),
}
_DEFAULT_PRICING = (300.0, 1500.0)

# Erros que não melhoram com nova tentativa nem em outro provedor.
_FATAL = ("authentication", "permission", "invalid_request", "not_found")


class AnthropicProvider:
    name: str
    requires_network = True

    def __init__(self, name: str, cfg: dict[str, Any]):
        self.name = name
        self.api_key = cfg.get("api_key")
        self.model = cfg.get("model", "claude-sonnet-5")
        self.max_tokens = int(cfg.get("max_tokens", 16000))
        # 'high' é o padrão da API. Para resumir transcrições, 'medium'
        # costuma bastar e sai bem mais barato.
        self.effort = cfg.get("effort", "medium")
        self.timeout = float(cfg.get("timeout_seconds", 300))
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                from anthropic import AsyncAnthropic
            except ImportError as exc:
                raise ProviderError(
                    "pacote 'anthropic' não instalado", provider=self.name, retryable=False
                ) from exc
            self._client = AsyncAnthropic(api_key=self.api_key, timeout=self.timeout)
        return self._client

    async def complete(
        self, prompt: str, *, system: str | None = None, max_tokens: int | None = None
    ) -> Completion:
        if not self.api_key:
            raise ProviderError("api_key ausente", provider=self.name, retryable=False)

        client = self._get_client()
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens or self.max_tokens,
            "messages": [{"role": "user", "content": prompt}],
            # Deixa o modelo decidir quanto pensar; 'effort' controla o gasto.
            # temperature/top_p foram removidos nos modelos atuais e devolvem 400.
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": self.effort},
        }
        if system:
            kwargs["system"] = system

        try:
            # Streaming: os relatórios podem passar de 10 min de geração, e o
            # SDK recusa requisições não-streaming com max_tokens alto.
            async with client.messages.stream(**kwargs) as stream:
                message = await stream.get_final_message()
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            fatal = any(word in detail.lower() for word in _FATAL)
            raise ProviderError(detail, provider=self.name, retryable=not fatal) from exc

        # Recusa por segurança: o conteúdo vem vazio. Não é erro de rede — tentar
        # outro provedor pode funcionar, então deixamos a cadeia seguir.
        if message.stop_reason == "refusal":
            raise ProviderError(
                "modelo recusou o pedido por política de segurança", provider=self.name
            )

        text = "".join(
            block.text for block in message.content if getattr(block, "type", "") == "text"
        ).strip()

        tokens_in = message.usage.input_tokens
        tokens_out = message.usage.output_tokens
        return Completion(
            text=text,
            provider=self.name,
            model=message.model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_cents=self.estimate_cost_cents(tokens_in, tokens_out),
            stop_reason=message.stop_reason,
        )

    async def health(self) -> bool:
        if not self.api_key:
            return False
        try:
            from importlib.util import find_spec

            return find_spec("anthropic") is not None
        except Exception:
            return False

    def estimate_cost_cents(self, tokens_in: int, tokens_out: int) -> float:
        price_in, price_out = _PRICING.get(self.model, _DEFAULT_PRICING)
        return round(
            (tokens_in / 1_000_000) * price_in + (tokens_out / 1_000_000) * price_out, 4
        )
