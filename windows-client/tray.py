"""Ícone de bandeja do Lifelog.

Roda a captura em segundo plano com um controle de pausa sempre à mão — que é
o ponto: gravação contínua sem um jeito óbvio de interromper é um problema de
privacidade, não um recurso.

    python windows-client/tray.py
"""

from __future__ import annotations

import logging
import sys
import threading
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pystray  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from runner import CaptureRunner  # noqa: E402

log = logging.getLogger("tray")

# Vermelho gravando, cinza pausado — legível de relance na barra de tarefas.
COLOR_RECORDING = (200, 70, 60)
COLOR_PAUSED = (130, 130, 130)
COLOR_ERROR = (190, 150, 40)


def _make_icon(color: tuple[int, int, int], *, paused: bool = False) -> Image.Image:
    """Desenha o ícone: círculo cheio gravando, dois traços quando pausado."""
    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse([4, 4, size - 4, size - 4], fill=color)

    if paused:
        # Símbolo de pausa vazado no meio do círculo.
        draw.rectangle([22, 20, 29, 44], fill=(255, 255, 255, 230))
        draw.rectangle([35, 20, 42, 44], fill=(255, 255, 255, 230))

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
        self._icon.icon = _make_icon(
            COLOR_ERROR if self.error else (COLOR_PAUSED if paused else COLOR_RECORDING),
            paused=paused,
        )
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

    def _boot(self, icon: pystray.Icon) -> None:
        """Sobe a captura depois que o ícone aparece.

        Carregar o VAD e abrir os dispositivos leva alguns segundos; fazer isso
        antes do ícone deixaria a bandeja vazia nesse intervalo.
        """
        icon.visible = True
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
            "lifelog", _make_icon(COLOR_PAUSED, paused=True), "Lifelog — iniciando…", menu
        )
        self._icon.run(setup=self._boot)
        return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    return LifelogTray().run()


if __name__ == "__main__":
    sys.exit(main())
