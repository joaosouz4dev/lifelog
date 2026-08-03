"""Sons curtos que dizem em que ponto o ditado está.

Sem retorno sonoro não há como saber se a tecla pegou: a pessoa fala achando
que está gravando e descobre que não estava só quando o texto não aparece.

Toca pelo ffplay, que já vem com o ffmpeg usado na captura. As APIs do
Windows não servem nesta classe de máquina: `Beep` fala com o alto-falante da
placa-mãe, que muitos PCs não têm, e `PlaySound` falhou com "Failed to play
sound" quando o dispositivo padrão é USB — caso comum com headset sem fio.

Os arquivos são gerados uma vez, no primeiro uso, e reaproveitados.
"""

from __future__ import annotations

import logging
import math
import struct
import subprocess
import tempfile
import threading
import wave
from pathlib import Path

log = logging.getLogger("sounds")

TAXA = 22050

# Notas de escala pentatônica: soam bem em qualquer ordem e não lembram
# alerta de sistema. Subindo = começou, descendo = terminou.
INICIO = ((587.33, 0.08), (880.00, 0.12))       # ré → lá
FIM = ((880.00, 0.08), (587.33, 0.12))          # lá → ré
CANCELADO = ((440.00, 0.10),)                   # lá grave, uma nota só
ERRO = ((392.00, 0.14), (293.66, 0.22))         # sol → ré grave

_PASTA = Path(tempfile.gettempdir()) / "lifelog-sons"
_ARQUIVOS: dict[int, Path] = {}
_lock = threading.Lock()


def _gerar(notas: tuple[tuple[float, float], ...], destino: Path) -> None:
    """Escreve um WAV com as notas em sequência.

    Envelope de ataque e decaimento: cortar a onda no meio produz um clique
    seco, que é justamente o som desagradável a evitar. Um harmônico fraco
    dá corpo sem soar metálico.
    """
    amostras = bytearray()

    for frequencia, duracao in notas:
        total = int(TAXA * duracao)
        ataque = max(1, int(total * 0.15))
        decaimento = max(1, int(total * 0.35))

        for i in range(total):
            if i < ataque:
                volume = i / ataque
            elif i > total - decaimento:
                volume = (total - i) / decaimento
            else:
                volume = 1.0

            t = i / TAXA
            valor = (
                math.sin(2 * math.pi * frequencia * t)
                + 0.25 * math.sin(4 * math.pi * frequencia * t)
            ) / 1.25
            amostras += struct.pack("<h", int(valor * volume * 14000))

    destino.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(destino), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(TAXA)
        wav.writeframes(bytes(amostras))


def _arquivo(notas: tuple[tuple[float, float], ...]) -> Path:
    with _lock:
        chave = id(notas)
        caminho = _ARQUIVOS.get(chave)
        if caminho is None or not caminho.exists():
            caminho = _PASTA / f"{chave}.wav"
            _gerar(notas, caminho)
            _ARQUIVOS[chave] = caminho
        return caminho


def _tocar(notas: tuple[tuple[float, float], ...]) -> None:
    try:
        subprocess.run(
            ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet",
             str(_arquivo(notas))],
            capture_output=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        log.debug("não deu para tocar o som", exc_info=True)


def tocar(notas: tuple[tuple[float, float], ...]) -> None:
    """Toca sem bloquear quem chamou.

    O ffplay é síncrono: chamado na thread do ditado, atrasaria o início da
    gravação e engoliria a primeira sílaba.
    """
    threading.Thread(target=_tocar, args=(notas,), daemon=True).start()
