"""Protocolos que todo provedor do hub implementa.

Um provedor é qualquer coisa que saiba transcrever áudio (STT) ou completar
texto (LLM). O hub não conhece detalhes de nenhum deles — só esta interface.
Adicionar um provedor novo é escrever uma classe e registrá-la no config.yaml.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from ..models import Completion, Transcript


class ProviderError(Exception):
    """Falha de um provedor.

    `retryable=False` sinaliza um erro que não melhora com nova tentativa
    (áudio corrompido, chave inválida) — o hub aborta a cadeia em vez de
    queimar os outros provedores com a mesma requisição inválida.
    """

    def __init__(self, message: str, *, provider: str = "", retryable: bool = True):
        super().__init__(message)
        self.provider = provider
        self.retryable = retryable


class BudgetExceeded(Exception):
    """Teto de gasto diário atingido."""


@runtime_checkable
class STTProvider(Protocol):
    name: str
    requires_network: bool

    async def transcribe(self, audio: Path, language: str = "pt") -> Transcript: ...

    async def health(self) -> bool: ...

    def estimate_cost_cents(self, duration_ms: int) -> float: ...


@runtime_checkable
class LLMProvider(Protocol):
    name: str
    requires_network: bool

    async def complete(
        self, prompt: str, *, system: str | None = None, max_tokens: int = 4096
    ) -> Completion: ...

    async def health(self) -> bool: ...

    def estimate_cost_cents(self, tokens_in: int, tokens_out: int) -> float: ...
