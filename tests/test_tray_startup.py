"""Testes do aviso de instância já em execução.

O aviso é um `MessageBoxW` modal: útil quando a pessoa clicou no ícone e nada
aconteceu, mas um estorvo se aparecer sozinho a cada logon — que foi o que
acontecia com dois caminhos de auto-start ativos ao mesmo tempo.
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


@pytest.fixture
def ja_rodando(monkeypatch):
    """Simula outra instância viva e espiona o aviso."""
    chamadas: list[bool] = []

    monkeypatch.setattr(tray.single_instance, "acquire", lambda: False)
    monkeypatch.setattr(tray, "_warn_already_running", lambda: chamadas.append(True))
    return chamadas


def test_inicio_automatico_nao_mostra_popup(ja_rodando):
    """No logon ninguém pediu para abrir — um popup modal seria só estorvo."""
    assert tray.main(["--startup"]) == 0
    assert ja_rodando == [], "não deveria avisar em início automático"


def test_abertura_manual_avisa_que_ja_esta_rodando(ja_rodando):
    """Aqui o aviso é útil: sem ele, clicar no ícone parece não fazer nada."""
    assert tray.main([]) == 0
    assert ja_rodando == [True]


def test_a_flag_e_reconhecida_entre_outros_argumentos(ja_rodando):
    """Atalhos do Windows podem injetar argumentos extras."""
    tray.main(["--startup", "--seja-la-o-que-for"])
    tray.main(["--outro", "--startup"])

    assert ja_rodando == [], "a posição da flag não deveria importar"


def test_sem_argumentos_explicitos_le_a_linha_de_comando(ja_rodando, monkeypatch):
    """É assim que o executável empacotado chega aqui."""
    monkeypatch.setattr(tray.sys, "argv", ["Lifelog.exe", "--startup"])

    assert tray.main() == 0
    assert ja_rodando == []
