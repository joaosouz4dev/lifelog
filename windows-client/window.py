"""Janela nativa da interface.

Abrir a interface como aba do navegador tem dois problemas: a pessoa precisa
saber a URL, e a janela se perde entre as outras abas. Uma janela própria
aparece na barra de tarefas com o ícone do Lifelog e se comporta como
aplicativo.

Usa o WebView2, que já vem instalado no Windows 11 — nada extra para baixar.
O servidor continua na porta 8000, então o acesso pelo celular na rede local
segue funcionando.

A janela roda num processo separado de propósito: o WebView2 exige a thread
principal, e a bandeja também. Um processo por dono resolve sem disputa.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

log = logging.getLogger("window")

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_URL = "http://127.0.0.1:8000"


def open_window(url: str = DEFAULT_URL) -> int:
    """Abre a interface numa janela nativa. Bloqueia até fecharem."""
    try:
        import webview
    except ImportError:
        # Sem pywebview, cair para o navegador é melhor que não abrir nada.
        log.info("pywebview ausente — abrindo no navegador")
        import webbrowser

        webbrowser.open(url)
        return 0

    try:
        webview.create_window(
            "Lifelog",
            url,
            width=1180,
            height=820,
            min_size=(900, 600),
            # A interface já tem tema escuro; sem isto a janela pisca branco
            # antes do primeiro paint.
            background_color="#17161A",
        )
        webview.start(icon=str(ROOT / "assets" / "icon.ico"))
        return 0
    except Exception:
        log.exception("falha ao abrir a janela nativa; caindo para o navegador")
        import webbrowser

        webbrowser.open(url)
        return 1


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    return open_window(url)


if __name__ == "__main__":
    sys.exit(main())
