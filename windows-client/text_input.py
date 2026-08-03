"""Escreve texto no campo que estiver em foco.

Usa `SendInput` com `KEYEVENTF_UNICODE`: nesse modo o código da tecla é 0 e o
caractere viaja no campo de scan, então funciona com acentos e independe do
layout de teclado — requisito real para português.

Textos longos vão pelo clipboard com Ctrl+V: digitar caractere a caractere
fica visivelmente lento e alguns campos com debounce perdem letras pelo
caminho. O conteúdo anterior do clipboard é restaurado, porque destruir o que
a pessoa tinha copiado é um estrago silencioso.

Tudo em ctypes puro: `pywin32` já está nas dependências, e evitar biblioteca
nova poupa uma entrada na lista frágil de HIDDEN_IMPORTS do build.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import time

log = logging.getLogger("typer")

# Acima disto a digitação caractere a caractere fica lenta demais.
LIMITE_PARA_CLIPBOARD = 200

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK_CONTROL = 0x11
VK_V = 0x56

try:
    _user32 = ctypes.windll.user32
    _DISPONIVEL = True
except Exception:  # pragma: no cover - só falha fora do Windows
    _DISPONIVEL = False


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wt.WORD),
        ("wScan", wt.WORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wt.ULONG)),
    ]


class _MOUSEINPUT(ctypes.Structure):
    """Não é usado aqui, mas precisa existir.

    `SendInput` valida o tamanho da struct que recebe, e quem define esse
    tamanho é o maior membro da união — o MOUSEINPUT, com 32 bytes contra 24
    do teclado. Sem ele a struct sai com 32 bytes em vez de 40, e a chamada
    falha com ERROR_INVALID_PARAMETER (87) sem digitar nada.
    """

    _fields_ = [
        ("dx", wt.LONG),
        ("dy", wt.LONG),
        ("mouseData", wt.DWORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wt.ULONG)),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("union", _INPUTUNION)]


def _evento(vk: int, scan: int, flags: int) -> _INPUT:
    return _INPUT(
        type=INPUT_KEYBOARD,
        union=_INPUTUNION(ki=_KEYBDINPUT(vk, scan, flags, 0, None)),
    )


def _enviar(eventos: list[_INPUT]) -> bool:
    """Envia os eventos. False se o Windows recusou.

    Conferir o retorno é obrigatório: `SendInput` devolve quantos eventos
    aceitou, e uma struct malformada faz ele devolver 0 sem lançar nada. Foi
    assim que uma união sem o MOUSEINPUT quebrou toda a digitação em silêncio
    — o ditado transcrevia e o texto sumia.
    """
    if not eventos:
        return True
    array = (_INPUT * len(eventos))(*eventos)
    aceitos = _user32.SendInput(len(eventos), array, ctypes.sizeof(_INPUT))
    if aceitos != len(eventos):
        erro = ctypes.windll.kernel32.GetLastError()
        log.warning(
            "o Windows aceitou %s de %s eventos (erro %s)%s",
            aceitos, len(eventos), erro,
            " — janela com privilégio maior?" if erro == 5 else "",
        )
        return False
    return True


def digitar(texto: str) -> bool:
    """Escreve o texto no campo focado. False se não deu para escrever."""
    if not _DISPONIVEL or not texto:
        return False

    if len(texto) > LIMITE_PARA_CLIPBOARD:
        return colar(texto)

    try:
        eventos: list[_INPUT] = []
        # Unidades UTF-16, não code points: o campo wScan tem 16 bits, então
        # um emoji precisa dos dois surrogates em sequência.
        for code in _unidades_utf16(texto):
            eventos.append(_evento(0, code, KEYEVENTF_UNICODE))
            eventos.append(_evento(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))

        if _enviar(eventos):
            return True
        # O alvo recusou a digitação (janela elevada, por exemplo). O
        # clipboard costuma passar onde o SendInput não passa.
        log.info("digitação recusada; tentando pelo clipboard")
        return colar(texto)
    except Exception:
        log.exception("falha ao digitar; tentando pelo clipboard")
        return colar(texto)


def _unidades_utf16(texto: str):
    """Cada unidade de 16 bits do texto, na ordem."""
    dados = texto.encode("utf-16-le")
    for i in range(0, len(dados), 2):
        yield dados[i] | (dados[i + 1] << 8)


def copiar(texto: str) -> bool:
    """Só põe no clipboard, sem colar. Usado quando o alvo é incerto."""
    try:
        import win32clipboard

        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, texto)
        finally:
            win32clipboard.CloseClipboard()
        return True
    except Exception:
        log.exception("falha ao copiar para o clipboard")
        return False


def colar(texto: str) -> bool:
    """Cola via Ctrl+V, restaurando o que estava no clipboard antes."""
    try:
        import win32clipboard
    except ImportError:
        return False

    anterior = None
    try:
        win32clipboard.OpenClipboard()
        try:
            if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                anterior = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()
    except Exception:
        log.debug("não deu para ler o clipboard anterior", exc_info=True)

    if not copiar(texto):
        return False

    try:
        _enviar([
            _evento(VK_CONTROL, 0, 0),
            _evento(VK_V, 0, 0),
            _evento(VK_V, 0, KEYEVENTF_KEYUP),
            _evento(VK_CONTROL, 0, KEYEVENTF_KEYUP),
        ])
    except Exception:
        log.exception("falha ao enviar Ctrl+V")
        return False

    # O app precisa de um instante para ler o clipboard antes de restaurarmos.
    if anterior is not None:
        time.sleep(0.25)
        copiar(anterior)
    return True
