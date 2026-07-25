"""Carregamento de configuração.

Lê config.yaml, sobrepõe config.local.yaml (não versionado) e resolve
referências ${VAR} contra o ambiente. Segredos nunca ficam no YAML versionado.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

_ENV_REF = re.compile(r"\$\{([A-Z0-9_]+)\}")

ROOT = Path(__file__).resolve().parent.parent


def _resolve_env(value: Any) -> Any:
    """Substitui ${VAR} pelo valor do ambiente, recursivamente.

    Uma referência a variável inexistente vira None em vez de erro: assim um
    provedor sem chave configurada é simplesmente pulado pelo hub, e não
    derruba o carregamento inteiro.
    """
    if isinstance(value, str):
        match = _ENV_REF.fullmatch(value.strip())
        if match:
            return os.environ.get(match.group(1))
        return _ENV_REF.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _resolve_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env(v) for v in value]
    return value


def _deep_merge(base: dict, override: dict) -> dict:
    """Merge recursivo. Listas são substituídas, não concatenadas."""
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class Config:
    """Acesso à configuração por caminho pontuado."""

    def __init__(self, data: dict):
        self._data = data

    def get(self, path: str, default: Any = None) -> Any:
        """Lê um valor aninhado: cfg.get("stt.chain")."""
        node: Any = self._data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def require(self, path: str) -> Any:
        value = self.get(path)
        if value is None:
            raise KeyError(f"configuração obrigatória ausente: {path}")
        return value

    def resolve_path(self, path: str, default: str) -> Path:
        """Resolve um caminho da config relativo à raiz do projeto."""
        raw = self.get(path, default)
        candidate = Path(raw).expanduser()
        return candidate if candidate.is_absolute() else (ROOT / candidate).resolve()

    @property
    def data(self) -> dict:
        return self._data


def load_config(path: Path | None = None) -> Config:
    base_path = path or (ROOT / "config.yaml")
    with open(base_path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    local_path = base_path.with_name("config.local.yaml")
    if local_path.exists():
        with open(local_path, encoding="utf-8") as fh:
            data = _deep_merge(data, yaml.safe_load(fh) or {})

    return Config(_resolve_env(data))


_cached: Config | None = None


def get_config() -> Config:
    global _cached
    if _cached is None:
        _cached = load_config()
    return _cached
