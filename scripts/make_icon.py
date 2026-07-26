"""Gera o ícone do Lifelog.

Desenha em vez de embarcar um binário: o .ico fica reprodutível, versionável
por diff e ajustável sem editor gráfico.

A marca é uma onda sonora dentro de um círculo — captura de áudio contínua.
O círculo fica cheio quando está gravando e ganha o símbolo de pausa quando
parado, que é o mesmo par de estados que a bandeja mostra.

    python scripts/make_icon.py
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"

# Terracota do tema da interface (--accent no app.css), para o ícone e a
# aplicação parecerem a mesma coisa.
ACCENT = (180, 95, 63)
ACCENT_DARK = (150, 72, 45)
PAUSED = (120, 120, 124)
INK = (250, 249, 247)

# Tamanhos que o Windows pede: bandeja (16), lista (32), ícone grande (48),
# e a visão extra-grande do Explorer (256).
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)


def draw_icon(size: int, *, paused: bool = False, for_tray: bool = False) -> Image.Image:
    """Desenha o ícone num tamanho.

    `for_tray` deixa a onda mais grossa: a 16px os traços finos somem contra
    a barra de tarefas.
    """
    # Desenha grande e reduz no fim — o antialiasing do Pillow no resize é
    # bem melhor que desenhar direto em 16px.
    scale = 8
    px = size * scale
    image = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    margin = px * 0.06
    body = (margin, margin, px - margin, px - margin)

    base = PAUSED if paused else ACCENT
    draw.ellipse(body, fill=base)

    # Um anel mais escuro dá volume sem precisar de gradiente.
    ring = px * 0.035
    draw.ellipse(body, outline=ACCENT_DARK if not paused else (95, 95, 99), width=int(ring))

    if paused:
        # Duas barras verticais, o símbolo universal de pausa.
        bar_w = px * 0.10
        bar_h = px * 0.30
        gap = px * 0.08
        cx, cy = px / 2, px / 2
        for direction in (-1, 1):
            x = cx + direction * (gap / 2 + (bar_w if direction > 0 else 0)) - (
                bar_w if direction < 0 else 0
            )
            draw.rounded_rectangle(
                [x, cy - bar_h / 2, x + bar_w, cy + bar_h / 2],
                radius=bar_w * 0.3,
                fill=INK,
            )
        return image.resize((size, size), Image.LANCZOS)

    # Onda sonora: barras verticais de alturas variadas, como um medidor de
    # nível. Alturas escolhidas para dar ritmo, não geradas aleatoriamente —
    # o ícone precisa ser sempre igual.
    heights = (0.30, 0.58, 0.86, 0.58, 0.30)
    bar_w = px * (0.11 if for_tray else 0.085)
    spacing = px * 0.145
    total = spacing * (len(heights) - 1)
    cx, cy = px / 2, px / 2

    for i, factor in enumerate(heights):
        x = cx - total / 2 + i * spacing
        h = px * 0.34 * factor
        draw.rounded_rectangle(
            [x - bar_w / 2, cy - h, x + bar_w / 2, cy + h],
            radius=bar_w / 2,
            fill=INK,
        )

    return image.resize((size, size), Image.LANCZOS)


def main() -> int:
    ASSETS.mkdir(parents=True, exist_ok=True)

    # .ico multi-resolução: o Windows escolhe o tamanho certo por contexto.
    frames = [draw_icon(s, for_tray=s <= 32) for s in ICO_SIZES]
    ico = ASSETS / "icon.ico"
    frames[-1].save(ico, format="ICO", sizes=[(s, s) for s in ICO_SIZES])
    print(f"  {ico.relative_to(ROOT)}  ({', '.join(str(s) for s in ICO_SIZES)})")

    # PNGs para a bandeja (que troca de ícone em runtime) e para o README.
    for name, paused in (("tray-recording.png", False), ("tray-paused.png", True)):
        path = ASSETS / name
        draw_icon(256, paused=paused, for_tray=True).save(path)
        print(f"  {path.relative_to(ROOT)}")

    banner = ASSETS / "icon-512.png"
    draw_icon(512).save(banner)
    print(f"  {banner.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
