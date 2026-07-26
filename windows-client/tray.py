"""Ícone de bandeja do Lifelog.

Roda a captura em segundo plano com um controle de pausa sempre à mão — que é
o ponto: gravação contínua sem um jeito óbvio de interromper é um problema de
privacidade, não um recurso.

    python windows-client/tray.py
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pystray  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

import single_instance  # noqa: E402
from runner import CaptureRunner  # noqa: E402

log = logging.getLogger("tray")

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"

# Âmbar para erro — não existe como arquivo porque é um estado raro; os dois
# principais (gravando e pausado) vêm dos PNGs gerados por scripts/make_icon.py.
COLOR_ERROR = (190, 150, 40)

_icon_cache: dict[str, Image.Image] = {}


def _make_icon(*, paused: bool = False, error: bool = False) -> Image.Image:
    """Ícone da bandeja, lido de assets/ com fallback desenhado.

    O fallback importa: sob PyInstaller o caminho de assets/ muda, e uma
    bandeja sem ícone é pior que uma com ícone feio.
    """
    key = "error" if error else ("paused" if paused else "recording")
    if key in _icon_cache:
        return _icon_cache[key]

    if not error:
        path = ASSETS / ("tray-paused.png" if paused else "tray-recording.png")
        if path.exists():
            image = Image.open(path).convert("RGBA").resize((64, 64), Image.LANCZOS)
            _icon_cache[key] = image
            return image

    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    color = COLOR_ERROR if error else ((120, 120, 124) if paused else (180, 95, 63))
    draw.ellipse([4, 4, size - 4, size - 4], fill=color)
    if paused:
        draw.rectangle([22, 20, 29, 44], fill=(250, 249, 247))
        draw.rectangle([35, 20, 42, 44], fill=(250, 249, 247))
    _icon_cache[key] = image
    return image


class LifelogTray:
    def __init__(self) -> None:
        self.runner: CaptureRunner | None = None
        self.error: str | None = None
        self._icon: pystray.Icon | None = None
        self._lock = threading.Lock()

    # ─────────────────────────────── estado ───────────────────────────────

    def _status_line(self, _item=None) -> str:
        if self.error:
            return f"Erro: {self.error[:50]}"
        if self.runner is None:
            return "Iniciando…"

        s = self.runner.status()
        if s["paused"]:
            return "Pausado"
        parts = [f"{s['captured']} capturados", f"{s['sent']} enviados"]
        if s["pending"]:
            parts.append(f"{s['pending']} na fila")
        if s["stuck"]:
            parts.append(f"{s['stuck']} travados")
        return " · ".join(parts)

    def _sources_line(self, _item=None) -> str:
        if self.runner is None or not self.runner.tracks:
            return "Nenhuma trilha ativa"
        labels = {"mic": "microfone", "system": "sistema", "gadget": "gadget"}
        return "Capturando: " + " + ".join(
            labels.get(t.source, t.source) for t in self.runner.tracks
        )

    def _pause_label(self, _item=None) -> str:
        if self.runner is None:
            return "Pausar captura"
        return "Retomar captura" if self.runner.is_paused else "Pausar captura"

    def _refresh(self) -> None:
        if self._icon is None or self.runner is None:
            return
        paused = self.runner.is_paused
        self._icon.icon = _make_icon(paused=paused, error=bool(self.error))
        self._icon.title = f"Lifelog — {self._status_line()}"

    # ─────────────────────────────── ações ───────────────────────────────

    def _toggle_pause(self, _icon=None, _item=None) -> None:
        with self._lock:
            if self.runner is None:
                return
            paused = self.runner.toggle_pause()
        log.info("captura %s", "pausada" if paused else "retomada")
        self._refresh()

    def _open_ui(self, _icon=None, _item=None) -> None:
        url = self.runner.server_url if self.runner else "http://127.0.0.1:8000"
        webbrowser.open(url)

    def _quit(self, icon=None, _item=None) -> None:
        log.info("encerrando…")
        with self._lock:
            if self.runner is not None:
                self.runner.stop()
                self.runner = None
        if icon is not None:
            icon.stop()

    # ─────────────────────────────── ciclo ───────────────────────────────

    def _ensure_server(self) -> None:
        """Sobe o servidor se ele não estiver respondendo.

        No app empacotado a bandeja é o único processo que o usuário inicia —
        sem isto, a captura encheria a fila local sem nunca conseguir enviar.
        """
        import httpx

        url = self.runner.server_url if self.runner else "http://127.0.0.1:8000"
        try:
            if httpx.get(f"{url}/health", timeout=3).status_code == 200:
                return
        except Exception:
            pass

        exe = Path(sys.executable).parent / "LifelogServer.exe"
        if not exe.exists():
            log.info("servidor não encontrado em %s — inicie-o manualmente", exe)
            return

        log.info("iniciando o servidor…")
        try:
            subprocess.Popen(
                [str(exe)],
                cwd=str(exe.parent),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            log.exception("falha ao iniciar o servidor")

    def _boot(self, icon: pystray.Icon) -> None:
        """Sobe a captura depois que o ícone aparece.

        Carregar o VAD e abrir os dispositivos leva alguns segundos; fazer isso
        antes do ícone deixaria a bandeja vazia nesse intervalo.
        """
        icon.visible = True

        # Só no app empacotado: em desenvolvimento o servidor é iniciado à mão
        # ou pela tarefa agendada, e subir outro criaria conflito de porta.
        if getattr(sys, "frozen", False):
            self._ensure_server()

        try:
            runner = CaptureRunner()
            if not runner.available:
                raise RuntimeError("nenhum dispositivo de áudio disponível")
            runner.start()
            self.runner = runner
        except Exception as exc:
            log.exception("falha ao iniciar a captura")
            self.error = str(exc)
            self._refresh()
            return

        self._refresh()

        # Mantém o tooltip e a cor do ícone em dia enquanto roda.
        while self.runner is not None:
            if threading.Event().wait(5):
                break
            self._refresh()

    def run(self) -> int:
        menu = pystray.Menu(
            pystray.MenuItem(self._status_line, None, enabled=False),
            pystray.MenuItem(self._sources_line, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(self._pause_label, self._toggle_pause, default=True),
            pystray.MenuItem("Abrir interface", self._open_ui),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Sair", self._quit),
        )

        self._icon = pystray.Icon(
            "lifelog", _make_icon(paused=True), "Lifelog — iniciando…", menu
        )
        self._icon.run(setup=self._boot)
        return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Duas bandejas disputam o mesmo dispositivo de áudio e a mesma fila —
    # o resultado parece "não está capturando". Sair é melhor que competir.
    if not single_instance.acquire():
        log.info("o Lifelog já está rodando — veja o ícone na bandeja")
        _warn_already_running()
        return 0

    try:
        return LifelogTray().run()
    finally:
        single_instance.release()


def _warn_already_running() -> None:
    """Avisa que já há uma instância — sem isto, abrir o app não faz nada."""
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            None,
            "O Lifelog já está em execução.\n\n"
            "Procure o ícone na bandeja, ao lado do relógio.",
            "Lifelog",
            0x40,  # MB_ICONINFORMATION
        )
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
