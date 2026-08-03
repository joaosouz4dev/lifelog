"""Atalho global de teclado, com press e release.

Usa `RegisterHotKey`, que registra **uma** combinação com o sistema e não vê
mais nada. A alternativa (`pynput`) instala um hook de teclado de baixo nível
que enxerga todas as teclas digitadas na máquina, senhas inclusive — num app
que já grava áudio continuamente, embutir isso muda a natureza do produto, e
é o que faz antivírus marcar o binário.

O preço é que `RegisterHotKey` só avisa o *press*, nunca o *release*. Para
push-to-talk, o release vem de um polling de `GetAsyncKeyState`, que custa
quase nada a 25 Hz.

Roda em thread própria com seu próprio message pump: `WM_HOTKEY` é entregue à
thread que registrou, e a thread principal já está ocupada com o pump do
pystray, que não repassa essa mensagem.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import threading
import time

log = logging.getLogger("hotkey")

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000  # segurar não deve disparar em rajada

_MODIFICADORES = {
    "ctrl": MOD_CONTROL, "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN, "super": MOD_WIN,
}

# Só o que faz sentido como atalho de ditado; o resto cai no `ord()`.
_TECLAS = {
    "space": 0x20, "espaco": 0x20, "espaço": 0x20,
    "enter": 0x0D, "tab": 0x09, "esc": 0x1B,
    **{f"f{i}": 0x70 + i - 1 for i in range(1, 13)},
}

# 25 Hz: rápido o bastante para a soltura parecer instantânea, leve o
# bastante para não aparecer no gerenciador de tarefas.
INTERVALO_POLLING = 0.04


def parse(combinacao: str) -> tuple[int, int]:
    """'ctrl+shift+space' vira (modificadores, código da tecla)."""
    partes = [p.strip().lower() for p in combinacao.split("+") if p.strip()]
    if not partes:
        raise ValueError("combinação vazia")

    mods = 0
    tecla = None
    for parte in partes:
        if parte in _MODIFICADORES:
            mods |= _MODIFICADORES[parte]
        elif parte in _TECLAS:
            tecla = _TECLAS[parte]
        elif len(parte) == 1:
            tecla = ord(parte.upper())
        else:
            raise ValueError(f"tecla desconhecida: {parte}")

    if tecla is None:
        raise ValueError(f"nenhuma tecla principal em '{combinacao}'")
    return mods, tecla


class HotkeyListener(threading.Thread):
    """Chama `on_press` ao apertar e `on_release` ao soltar."""

    def __init__(self, combinacao: str, on_press, on_release, *, on_cancel=None):
        super().__init__(name="hotkey", daemon=True)
        self.combinacao = combinacao
        self.mods, self.tecla = parse(combinacao)
        self.on_press = on_press
        self.on_release = on_release
        self.on_cancel = on_cancel
        self.registrado = False
        self.erro: str | None = None
        self._pronto = threading.Event()
        self._parar = threading.Event()
        self._thread_id: int | None = None

    def aguardar_registro(self, timeout: float = 5.0) -> bool:
        self._pronto.wait(timeout)
        return self.registrado

    def run(self) -> None:
        user32 = ctypes.windll.user32
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()

        if not user32.RegisterHotKey(None, 1, self.mods | MOD_NOREPEAT, self.tecla):
            # Combinação já tomada por outro app. Não é motivo para derrubar
            # nada: a captura é a função principal, o ditado é acessório.
            self.erro = f"'{self.combinacao}' já está em uso por outro programa"
            log.warning("ditado indisponível: %s", self.erro)
            self._pronto.set()
            return

        self.registrado = True
        self._pronto.set()
        log.info("ditado armado em %s", self.combinacao)

        msg = wt.MSG()
        try:
            while not self._parar.is_set():
                if not user32.GetMessageW(ctypes.byref(msg), None, 0, 0):
                    break
                if msg.message == WM_HOTKEY:
                    self._ciclo_da_tecla(user32)
        finally:
            user32.UnregisterHotKey(None, 1)
            log.info("ditado desarmado")

    def _ciclo_da_tecla(self, user32) -> None:
        """Press → espera soltar → release. Bloqueia esta thread de propósito.

        Enquanto a tecla está pressionada não há outro atalho a atender, e
        segurar o pump aqui evita reentrância no controlador.
        """
        try:
            self.on_press()
        except Exception:
            log.exception("falha no início do ditado")
            return

        cancelado = False
        while not self._parar.is_set():
            # 0x8000 é o bit de "pressionada agora".
            if not (user32.GetAsyncKeyState(self.tecla) & 0x8000):
                break
            if self.on_cancel is not None and (user32.GetAsyncKeyState(0x1B) & 0x8000):
                cancelado = True  # Esc no meio da fala
                break
            time.sleep(INTERVALO_POLLING)

        # Segurar a tecla enfileira WM_HOTKEY repetidos, mesmo com
        # MOD_NOREPEAT. Sem descartá-los, cada um vira um ditado novo: uma
        # única fala saía como três transcrições coladas no campo.
        descartados = self._drenar_repeticoes(user32)
        if descartados:
            log.debug("descartadas %s repetições do atalho", descartados)

        try:
            if cancelado:
                self.on_cancel()
            else:
                self.on_release()
        except Exception:
            log.exception("falha no fim do ditado")

    @staticmethod
    def _drenar_repeticoes(user32) -> int:
        """Remove da fila os WM_HOTKEY acumulados enquanto a tecla estava presa."""
        msg = wt.MSG()
        removidos = 0
        # PM_REMOVE = 0x0001: tira a mensagem da fila em vez de só espiar.
        while user32.PeekMessageW(
            ctypes.byref(msg), None, WM_HOTKEY, WM_HOTKEY, 0x0001
        ):
            removidos += 1
        return removidos

    def stop(self) -> None:
        self._parar.set()
        if self._thread_id is not None:
            # Acorda o GetMessageW, que de outro modo ficaria bloqueado até a
            # próxima mensagem chegar — e o processo não encerraria.
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
