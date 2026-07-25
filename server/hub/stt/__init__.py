"""Provedores de transcrição.

Para adicionar um provedor: escreva a classe, registre em _REGISTRY e
referencie no config.yaml. Nada mais no código precisa mudar.
"""

from __future__ import annotations

import logging
from typing import Any

from ...config import Config
from ..chain import ProviderChain
from .deepgram_provider import DeepgramProvider
from .faster_whisper_provider import FasterWhisperProvider

log = logging.getLogger(__name__)

_REGISTRY: dict[str, Any] = {
    "faster_whisper": FasterWhisperProvider,
    "deepgram": DeepgramProvider,
}


def build_stt_chain(cfg: Config) -> ProviderChain:
    """Monta a cadeia de STT a partir da configuração.

    Provedores desativados, sem tipo conhecido ou que falham ao instanciar são
    pulados com aviso — a cadeia sobe com o que der, em vez de não subir.
    """
    chain_names: list[str] = cfg.get("stt.chain", []) or []
    providers_cfg: dict[str, dict] = cfg.get("stt.providers", {}) or {}

    providers = []
    for name in chain_names:
        pcfg = providers_cfg.get(name)
        if pcfg is None:
            log.warning("stt: provedor '%s' está na cadeia mas não foi configurado", name)
            continue
        if not pcfg.get("enabled", True):
            log.info("stt: provedor '%s' desativado", name)
            continue

        cls = _REGISTRY.get(pcfg.get("type", ""))
        if cls is None:
            log.warning("stt: tipo desconhecido '%s' para '%s'", pcfg.get("type"), name)
            continue

        try:
            providers.append(cls(name, pcfg))
        except Exception:
            log.exception("stt: falha ao instanciar '%s'", name)

    if not providers:
        log.error("stt: nenhum provedor disponível — transcrição não vai funcionar")

    return ProviderChain(
        "stt",
        providers,
        daily_budget_cents=float(cfg.get("stt.daily_budget_cents", 0) or 0),
        failure_threshold=int(cfg.get("stt.circuit_breaker.failure_threshold", 3)),
        reset_after_seconds=float(cfg.get("stt.circuit_breaker.reset_after_seconds", 300)),
    )


__all__ = ["build_stt_chain", "FasterWhisperProvider", "DeepgramProvider"]
