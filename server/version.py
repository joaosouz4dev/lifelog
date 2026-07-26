"""Versão do app e verificação de atualizações.

A versão é injetada pelo CI em `server/_version.py` a cada release. Rodando do
código-fonte esse arquivo não existe, e a versão vira "dev".
"""

from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger(__name__)

REPO = "joaosouz4dev/lifelog"
RELEASES_URL = f"https://github.com/{REPO}/releases/latest"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"

# Consultar a API do GitHub a cada carregamento da página gastaria a cota
# anônima (60/hora) à toa — a versão publicada muda raramente.
CACHE_SECONDS = 3600

_cache: dict | None = None
_cached_at = 0.0


def current() -> str:
    try:
        from ._version import __version__

        return __version__
    except ImportError:
        return "dev"


def _parse(version: str) -> tuple[int, ...]:
    """Converte '0.0.4' em (0, 0, 4) para comparar numericamente.

    Comparar as strings daria errado no primeiro '0.0.10' — que ordena antes
    de '0.0.9'.
    """
    parts = []
    for chunk in version.strip().lstrip("v").split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


async def check_update() -> dict:
    """Compara a versão local com a última publicada no GitHub."""
    global _cache, _cached_at

    running = current()

    if _cache is not None and time.monotonic() - _cached_at < CACHE_SECONDS:
        return {**_cache, "current": running}

    result = {
        "current": running,
        "latest": None,
        "update_available": False,
        "url": RELEASES_URL,
        "checked": False,
    }

    # Rodando do código-fonte não há o que atualizar por instalador.
    if running == "dev":
        return result

    try:
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.get(
                API_URL, headers={"accept": "application/vnd.github+json"}
            )
        if response.status_code == 200:
            latest = str(response.json().get("tag_name") or "").lstrip("v")
            if latest:
                result["latest"] = latest
                result["update_available"] = _parse(latest) > _parse(running)
                result["checked"] = True
    except Exception:
        # Sem internet, a interface simplesmente não mostra nada sobre update.
        log.debug("falha ao consultar a última versão", exc_info=True)

    _cache = result
    _cached_at = time.monotonic()
    return result
