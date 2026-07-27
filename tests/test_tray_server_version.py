"""Testes da troca do servidor quando a versão diverge.

Depois de atualizar, o servidor antigo continua ocupando a porta: a bandeja
nova vê o /health responder, não sobe o próprio, e os segmentos ficam presos
em 'pending' — capturados mas nunca transcritos.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

CLIENTE = Path(__file__).resolve().parent.parent / "windows-client"
if str(CLIENTE) not in sys.path:
    sys.path.insert(0, str(CLIENTE))

pytest.importorskip("pystray", reason="a bandeja só roda no Windows com pystray")

import tray  # noqa: E402


class BandejaFalsa:
    """Só as partes de tray.LifelogTray que a troca de servidor usa."""

    runner = None

    _server_version = tray.LifelogTray._server_version
    _stop_stale_server = tray.LifelogTray._stop_stale_server
    _ensure_server = tray.LifelogTray._ensure_server


@pytest.fixture
def bandeja(monkeypatch, tmp_path):
    b = BandejaFalsa()
    b.encerrou = False
    b.subiu = False

    monkeypatch.setattr(b, "_stop_stale_server", lambda: (setattr(b, "encerrou", True), True)[1])
    # Sem o .exe no disco a subida para antes de tentar; aponta para um que existe.
    exe = tmp_path / "LifelogServer.exe"
    exe.write_text("")
    monkeypatch.setattr(tray.sys, "executable", str(tmp_path / "Lifelog.exe"))
    monkeypatch.setattr(tray.subprocess, "Popen",
                        lambda *a, **k: setattr(b, "subiu", True))
    monkeypatch.setattr(tray.time, "sleep", lambda _: None)
    return b


def test_mesma_versao_nao_mexe_no_servidor(bandeja, monkeypatch):
    """Reiniciar um servidor que já está certo cortaria a captura à toa."""
    monkeypatch.setattr(bandeja, "_server_version", lambda url: "0.0.12")
    monkeypatch.setattr(tray, "_my_version", lambda: "0.0.12")

    bandeja._ensure_server()

    assert bandeja.encerrou is False
    assert bandeja.subiu is False


def test_versao_diferente_troca_o_servidor(bandeja, monkeypatch):
    """O caso real: sobrou o servidor da versão anterior na porta."""
    monkeypatch.setattr(bandeja, "_server_version", lambda url: "0.0.8")
    monkeypatch.setattr(tray, "_my_version", lambda: "0.0.12")

    bandeja._ensure_server()

    assert bandeja.encerrou is True, "devia ter encerrado o servidor antigo"
    assert bandeja.subiu is True, "devia ter subido o servidor novo"


def test_porta_livre_sobe_o_servidor(bandeja, monkeypatch):
    monkeypatch.setattr(bandeja, "_server_version", lambda url: None)
    monkeypatch.setattr(tray, "_my_version", lambda: "0.0.12")

    bandeja._ensure_server()

    assert bandeja.encerrou is False, "não há o que encerrar"
    assert bandeja.subiu is True


def test_servidor_sem_endpoint_de_versao_e_trocado(bandeja, monkeypatch):
    """Versão antiga demais para ter /api/version também precisa sair."""
    monkeypatch.setattr(bandeja, "_server_version", lambda url: "desconhecida")
    monkeypatch.setattr(tray, "_my_version", lambda: "0.0.12")

    bandeja._ensure_server()

    assert bandeja.encerrou is True
    assert bandeja.subiu is True


@pytest.mark.parametrize(
    ("no_ar", "minha"),
    [("0.0.12", "dev"), ("dev", "0.0.12")],
)
def test_dev_nunca_derruba_o_outro(bandeja, monkeypatch, no_ar, minha):
    """Rodar do código-fonte não pode matar o servidor do app instalado.

    A versão do código-fonte é sempre "dev", então divergiria de qualquer
    versão publicada — e as duas coexistem na máquina de quem desenvolve.
    """
    monkeypatch.setattr(bandeja, "_server_version", lambda url: no_ar)
    monkeypatch.setattr(tray, "_my_version", lambda: minha)

    bandeja._ensure_server()

    assert bandeja.encerrou is False
    assert bandeja.subiu is False


def test_se_nao_conseguir_encerrar_segue_com_o_antigo(bandeja, monkeypatch):
    """Melhor um servidor velho atendendo que nenhum — a fila não se perde."""
    monkeypatch.setattr(bandeja, "_server_version", lambda url: "0.0.8")
    monkeypatch.setattr(tray, "_my_version", lambda: "0.0.12")
    monkeypatch.setattr(bandeja, "_stop_stale_server", lambda: False)

    bandeja._ensure_server()

    assert bandeja.subiu is False, "duas instâncias disputariam a mesma porta"
