"""Provedores de LLM.

Mesmo padrão do hub de STT: escreva a classe, registre em _REGISTRY,
referencie no config.yaml.
"""

from __future__ import annotations

import logging
from typing import Any

from ...config import Config
from ..chain import ProviderChain
from .anthropic_provider import AnthropicProvider
from .echo_provider import EchoProvider
from .ollama_provider import OllamaProvider

log = logging.getLogger(__name__)

_REGISTRY: dict[str, Any] = {
    "anthropic": AnthropicProvider,
    "ollama": OllamaProvider,
    "echo": EchoProvider,
}


def build_llm_chain(cfg: Config) -> ProviderChain:
    """Monta a cadeia de LLM a partir da configuração.

    Provedores desativados, de tipo desconhecido ou que falham ao instanciar são
    pulados com aviso — a cadeia sobe com o que der.
    """
    chain_names: list[str] = cfg.get("llm.chain", []) or []
    providers_cfg: dict[str, dict] = cfg.get("llm.providers", {}) or {}

    providers = []
    for name in chain_names:
        pcfg = providers_cfg.get(name)
        if pcfg is None:
            log.warning("llm: provedor '%s' está na cadeia mas não foi configurado", name)
            continue
        if not pcfg.get("enabled", True):
            log.info("llm: provedor '%s' desativado", name)
            continue

        cls = _REGISTRY.get(pcfg.get("type", ""))
        if cls is None:
            log.warning("llm: tipo desconhecido '%s' para '%s'", pcfg.get("type"), name)
            continue

        try:
            providers.append(cls(name, pcfg))
        except Exception:
            log.exception("llm: falha ao instanciar '%s'", name)

    if not providers:
        log.info("llm: nenhum provedor ativo — relatórios e chat ficam indisponíveis")

    return ProviderChain(
        "llm",
        providers,
        daily_budget_cents=float(cfg.get("llm.daily_budget_cents", 0) or 0),
        failure_threshold=int(cfg.get("llm.circuit_breaker.failure_threshold", 3)),
        reset_after_seconds=float(cfg.get("llm.circuit_breaker.reset_after_seconds", 300)),
    )


__all__ = ["AnthropicProvider", "EchoProvider", "OllamaProvider", "build_llm_chain"]
