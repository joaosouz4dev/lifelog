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
import time
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


def _my_version() -> str:
    """Versão desta build — a mesma que o servidor irmão reporta."""
    try:
        from server.version import current

        return current()
    except Exception:
        return "dev"


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

    def _meeting_line(self, _item=None) -> str:
        """Diz por que a captura está parada — senão parece defeito."""
        if self.runner is None:
            return ""
        estado = self.runner.status().get("meeting")
        if estado is None:
            return ""  # gate desligado: a captura é contínua, nada a explicar
        if estado["em_reuniao"]:
            return f"Gravando: {estado['motivo'][:44]}"
        return f"Em espera: {estado['motivo'][:44]}"

    def _dictation_line(self, _item=None) -> str:
        """Sem isto não há como saber se o atalho pegou ou está tomado."""
        if self.runner is None:
            return "Ditado: iniciando…"
        estado = self.runner.status().get("dictation")
        if estado is None:
            return "Ditado: desativado"
        if not estado["armado"]:
            return f"Ditado indisponível: {estado['erro'] or 'falhou'}"
        atalho = estado["hotkey"].replace("+", " + ")
        return f"Ditado: segure {atalho}"

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
        """Abre a interface numa janela própria, com ícone na barra de tarefas.

        Num processo separado porque o WebView2 exige a thread principal, que
        aqui é da bandeja. Também isola travadas da janela da captura.
        """
        url = self.runner.server_url if self.runner else "http://127.0.0.1:8000"

        if getattr(sys, "frozen", False):
            exe = Path(sys.executable).parent / "LifelogUI.exe"
            cmd = [str(exe), url] if exe.exists() else None
        else:
            cmd = [sys.executable, str(Path(__file__).parent / "window.py"), url]

        if cmd:
            try:
                subprocess.Popen(
                    cmd,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                return
            except Exception:
                log.exception("falha ao abrir a janela; caindo para o navegador")

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

    def _server_version(self, url: str) -> str | None:
        """Versão do servidor que atende nesta porta, ou None se não atende."""
        import httpx

        try:
            if httpx.get(f"{url}/health", timeout=3).status_code != 200:
                return None
        except Exception:
            return None

        try:
            return httpx.get(f"{url}/api/version", timeout=3).json().get("current")
        except Exception:
            # Servidor antigo demais para ter o endpoint: responde ao /health
            # mas não sabe dizer a versão. Vale trocar do mesmo jeito.
            return "desconhecida"

    def _stop_stale_server(self) -> bool:
        """Encerra um servidor de outra versão preso na porta.

        Depois de atualizar, o servidor antigo continua no ar: a bandeja nova
        vê a porta ocupada, não sobe o próprio, e os segmentos ficam presos em
        'pending' — capturados mas nunca transcritos.
        """
        try:
            import psutil
        except ImportError:
            log.warning("psutil ausente — não dá para encerrar o servidor antigo")
            return False

        encerrados = 0
        for proc in psutil.process_iter(["name", "pid"]):
            if (proc.info["name"] or "").lower() != "lifelogserver.exe":
                continue
            try:
                proc.terminate()
                proc.wait(timeout=8)
                encerrados += 1
            except Exception:
                log.debug("falha ao encerrar o servidor pid=%s", proc.info["pid"])
        return encerrados > 0

    def _ensure_server(self) -> None:
        """Sobe o servidor se ele não estiver respondendo — ou se for de outra versão.

        No app empacotado a bandeja é o único processo que o usuário inicia —
        sem isto, a captura encheria a fila local sem nunca conseguir enviar.
        """
        url = self.runner.server_url if self.runner else "http://127.0.0.1:8000"
        no_ar = self._server_version(url)

        if no_ar is not None:
            minha = _my_version()
            if no_ar == minha or "dev" in (no_ar, minha):
                # "dev" é quem roda do código-fonte. Comparar com uma versão
                # publicada daria divergência sempre, e a bandeja de
                # desenvolvimento derrubaria o servidor do app instalado.
                return
            # Conferir só o /health deixaria passar o servidor da versão
            # anterior, que atende mas não fala o mesmo protocolo.
            log.info("servidor na porta é da versão %s (a minha é %s); trocando",
                     no_ar, minha)
            if not self._stop_stale_server():
                log.warning("não consegui encerrar o servidor antigo; seguindo com ele")
                return
            time.sleep(1.5)  # a porta leva um instante para liberar

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
            pystray.MenuItem(self._meeting_line, None, enabled=False),
            pystray.MenuItem(self._dictation_line, None, enabled=False),
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


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Início automático (atalho da inicialização, tarefa agendada). Aqui
    # ninguém pediu para abrir o app, então um aviso modal seria só um
    # estorvo a cada logon.
    automatico = "--startup" in (sys.argv[1:] if argv is None else argv)

    # Duas bandejas disputam o mesmo dispositivo de áudio e a mesma fila —
    # o resultado parece "não está capturando". Sair é melhor que competir.
    if not single_instance.acquire():
        log.info("o Lifelog já está rodando — veja o ícone na bandeja")
        if not automatico:
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
