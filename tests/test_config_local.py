"""Testes da gravação de configuração pela interface.

Ajustes feitos na tela vão para config.local.yaml, que sobrepõe o config.yaml
versionado. O detalhe que mais importa aqui é a invalidação do cache: sem ela
a mudança só valeria no próximo boot do servidor.
"""

from __future__ import annotations

import yaml

from server import config as config_mod


def _isolar(tmp_path, monkeypatch, base: dict) -> None:
    """Aponta a config para uma raiz descartável, com um config.yaml próprio."""
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(base, allow_unicode=True), encoding="utf-8"
    )
    monkeypatch.setattr(config_mod, "ROOT", tmp_path)
    monkeypatch.setattr(config_mod, "IS_FROZEN", False)
    config_mod.reset_cache()


def test_gravar_cria_o_arquivo_local(tmp_path, monkeypatch):
    _isolar(tmp_path, monkeypatch, {"capture": {"allowlist": ["meet"]}})

    destino = config_mod.save_local_override({"capture": {"allowlist": ["zoom"]}})

    assert destino == tmp_path / "config.local.yaml"
    assert destino.exists()


def test_o_cache_e_invalidado_depois_de_gravar(tmp_path, monkeypatch):
    """Sem isto, marcar uma opção na tela não mudaria nada até reiniciar."""
    _isolar(tmp_path, monkeypatch, {"capture": {"allowlist": ["meet"]}})

    assert config_mod.get_config().get("capture.allowlist") == ["meet"]

    config_mod.save_local_override({"capture": {"allowlist": ["zoom", "teams"]}})

    assert config_mod.get_config().get("capture.allowlist") == ["zoom", "teams"]


def test_a_lista_e_substituida_e_nao_concatenada(tmp_path, monkeypatch):
    """A tela manda a lista completa, não um acréscimo."""
    _isolar(tmp_path, monkeypatch, {"capture": {"allowlist": ["meet", "zoom"]}})

    config_mod.save_local_override({"capture": {"allowlist": ["teams"]}})

    assert config_mod.get_config().get("capture.allowlist") == ["teams"]


def test_gravar_preserva_o_que_ja_estava_no_arquivo(tmp_path, monkeypatch):
    """Salvar a allowlist não pode apagar a blocklist gravada antes."""
    _isolar(tmp_path, monkeypatch, {"capture": {}})

    config_mod.save_local_override({"capture": {"blocklist": ["1password"]}})
    config_mod.save_local_override({"capture": {"allowlist": ["meet"]}})

    cfg = config_mod.get_config()
    assert cfg.get("capture.blocklist") == ["1password"]
    assert cfg.get("capture.allowlist") == ["meet"]


def test_gravar_nao_toca_no_config_versionado(tmp_path, monkeypatch):
    """O config.yaml é versionado — a interface não pode reescrevê-lo."""
    _isolar(tmp_path, monkeypatch, {"capture": {"allowlist": ["meet"]}})
    antes = (tmp_path / "config.yaml").read_text(encoding="utf-8")

    config_mod.save_local_override({"capture": {"allowlist": ["zoom"]}})

    assert (tmp_path / "config.yaml").read_text(encoding="utf-8") == antes


def test_segredo_resolvido_nao_volta_para_o_disco(tmp_path, monkeypatch):
    """A API aceita patch, nunca cfg.data — que já tem os ${VAR} resolvidos."""
    monkeypatch.setenv("DEEPGRAM_API_KEY", "chave-secreta-de-verdade")
    _isolar(
        tmp_path, monkeypatch,
        {"stt": {"providers": {"deepgram": {"api_key": "${DEEPGRAM_API_KEY}"}}}},
    )

    # A chave está resolvida em memória…
    assert config_mod.get_config().get("stt.providers.deepgram.api_key") == (
        "chave-secreta-de-verdade"
    )

    destino = config_mod.save_local_override({"capture": {"allowlist": ["meet"]}})

    assert "chave-secreta-de-verdade" not in destino.read_text(encoding="utf-8")


def test_nao_deixa_arquivo_temporario_para_tras(tmp_path, monkeypatch):
    """A gravação é atômica; o .tmp é um detalhe interno."""
    _isolar(tmp_path, monkeypatch, {"capture": {}})

    config_mod.save_local_override({"capture": {"allowlist": ["meet"]}})

    assert list(tmp_path.glob("*.tmp")) == []
