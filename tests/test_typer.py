"""Testes da digitação no campo focado.

O `SendInput` é substituído por um espião: digitar de verdade durante um
teste mandaria texto para a janela que estivesse em foco.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

CLIENTE = Path(__file__).resolve().parent.parent / "windows-client"
if str(CLIENTE) not in sys.path:
    sys.path.insert(0, str(CLIENTE))

typer = pytest.importorskip("typer", reason="a digitação só existe no Windows")


@pytest.fixture
def eventos(monkeypatch):
    """Captura o que teria sido enviado ao Windows."""
    capturados: list[tuple[int, int, int]] = []

    def espiao(n, array, tamanho):
        for i in range(n):
            ki = array[i].union.ki
            capturados.append((ki.wVk, ki.wScan, ki.dwFlags))
        return n

    monkeypatch.setattr(
        typer, "_user32", type("U", (), {"SendInput": staticmethod(espiao)})()
    )
    return capturados


def _texto_enviado(eventos) -> str:
    """Reconstrói o texto a partir dos eventos de tecla pressionada."""
    unidades = [
        scan for _, scan, flags in eventos
        if flags & typer.KEYEVENTF_UNICODE and not flags & typer.KEYEVENTF_KEYUP
    ]
    dados = b"".join(u.to_bytes(2, "little") for u in unidades)
    return dados.decode("utf-16-le")


def test_digita_acentos_e_cedilha(eventos):
    """Sem KEYEVENTF_UNICODE isto dependeria do layout de teclado."""
    typer.digitar("Reunião às 10h — orçamento")

    assert _texto_enviado(eventos) == "Reunião às 10h — orçamento"


def test_o_codigo_de_tecla_e_sempre_zero(eventos):
    """No modo unicode o caractere viaja no scan, não no vk."""
    typer.digitar("olá")

    assert all(
        vk == 0 for vk, _, flags in eventos if flags & typer.KEYEVENTF_UNICODE
    )


def test_cada_caractere_tem_press_e_release(eventos):
    typer.digitar("abc")

    assert len(eventos) == 6


def test_emoji_vira_par_de_surrogates(eventos):
    """O campo de scan tem 16 bits; fora do BMP precisa de duas unidades."""
    typer.digitar("ok 🎉")

    unidades = [
        s for _, s, f in eventos
        if f & typer.KEYEVENTF_UNICODE and not f & typer.KEYEVENTF_KEYUP
    ]
    assert len(unidades) == 5, "3 do 'ok ' + 2 do surrogate"
    assert _texto_enviado(eventos) == "ok 🎉"


def test_texto_longo_vai_pelo_clipboard(eventos, monkeypatch):
    """Digitar 300 caracteres um a um fica visivelmente lento."""
    copiados: list[str] = []
    monkeypatch.setattr(typer, "copiar", lambda t: copiados.append(t) or True)

    typer.digitar("palavra " * 40)

    assert copiados, "deveria ter usado o clipboard"
    assert not any(f & typer.KEYEVENTF_UNICODE for _, _, f in eventos)


def test_texto_vazio_nao_envia_nada(eventos):
    assert typer.digitar("") is False
    assert eventos == []
