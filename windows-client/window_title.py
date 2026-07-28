"""Lê o título da janela de um processo que está emitindo som.

O nome do executável não basta: `msedge.exe` serve tanto uma reunião no Meet
quanto um filme na Netflix. O título da janela diz qual dos dois — os
navegadores põem ali o título da aba ativa:

    "Reunião — Google Meet - Google Chrome"
    "O Poderoso Chefão - Netflix - Google Chrome"

É informação que o Windows já expõe, sem extensão nem permissão extra.

Limite conhecido: só a aba *ativa* aparece no título. Um vídeo tocando numa
aba de fundo é rotulado com o título da aba que está à frente. Como o áudio
de fundo costuma ser justamente o que não interessa, o erro tende a marcar
como entretenimento algo que já seria descartado — e a lista de permitidos
protege contra transcrever o que não deve.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import threading
import time

log = logging.getLogger("window_title")

# Mesmo motivo do cache em audio_source: o título não muda a cada 100 ms, e
# varrer todas as janelas do sistema não é de graça.
CACHE_SECONDS = 1.5

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

try:
    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32
    _EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    _DISPONIVEL = True
except Exception:  # pragma: no cover - só falha fora do Windows
    _DISPONIVEL = False


def _nome_do_processo(hwnd) -> str:
    """Executável dono da janela, em minúsculas ('chrome.exe')."""
    pid = wt.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(512)
        tamanho = wt.DWORD(512)
        if not _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(tamanho)):
            return ""
        return buf.value.split("\\")[-1].lower()
    finally:
        _kernel32.CloseHandle(handle)


def _titulos_por_processo() -> dict[str, str]:
    """Título da maior janela visível de cada processo.

    A maior porque um navegador tem várias janelas — a de conteúdo é a que
    interessa, não um tooltip ou um popup de 1 pixel.
    """
    achados: dict[str, tuple[int, str]] = {}

    def callback(hwnd, _):
        if not _user32.IsWindowVisible(hwnd):
            return True
        tamanho = _user32.GetWindowTextLengthW(hwnd)
        if tamanho == 0:
            return True

        rect = wt.RECT()
        if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        area = (rect.right - rect.left) * (rect.bottom - rect.top)
        if area < 10_000:  # ~100x100: popups e tooltips não contam
            return True

        processo = _nome_do_processo(hwnd)
        if not processo:
            return True

        anterior = achados.get(processo)
        if anterior is None or area > anterior[0]:
            buf = ctypes.create_unicode_buffer(tamanho + 1)
            _user32.GetWindowTextW(hwnd, buf, tamanho + 1)
            achados[processo] = (area, buf.value)
        return True

    _user32.EnumWindows(_EnumWindowsProc(callback), None)
    return {processo: titulo for processo, (_, titulo) in achados.items()}


class WindowTitleProbe:
    """Consulta os títulos das janelas, com cache curto.

    Degrada em silêncio: sem título o segmento fica com o nome do executável,
    como antes.
    """

    def __init__(self, cache_seconds: float = CACHE_SECONDS):
        self.cache_seconds = cache_seconds
        self._lock = threading.Lock()
        self._cached: dict[str, str] = {}
        self._cached_at = 0.0

    def titles(self) -> dict[str, str]:
        if not _DISPONIVEL:
            return {}
        with self._lock:
            agora = time.monotonic()
            if agora - self._cached_at < self.cache_seconds:
                return self._cached
            try:
                self._cached = _titulos_por_processo()
            except Exception:
                log.debug("falha ao ler os títulos das janelas", exc_info=True)
                self._cached = {}
            self._cached_at = agora
            return self._cached

    def title_for(self, process: str) -> str | None:
        """Título da janela do processo, se houver."""
        return self.titles().get(process.lower())
