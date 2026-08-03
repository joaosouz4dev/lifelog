"""Bipes curtos que dizem em que ponto o ditado está.

Sem retorno sonoro não há como saber se a tecla pegou: a pessoa fala achando
que está gravando e descobre que não estava só quando o texto não aparece.

Usa `Beep` do kernel32 em vez de tocar um arquivo: dispara na hora, não
depende do dispositivo de saída padrão (que pode ser um fone desligado) e não
disputa a placa com a captura em curso — o loopback gravaria o próprio bipe.
"""

from __future__ import annotations

import ctypes
import logging
import threading

log = logging.getLogger("sounds")

# Duas notas subindo = começou; duas descendo = terminou. O padrão importa
# mais que a nota: dá para distinguir sem olhar a tela.
INICIO = ((880, 70), (1320, 70))
FIM = ((1320, 60), (880, 90))
ERRO = ((400, 160), (300, 200))
CANCELADO = ((700, 60),)


def _tocar(notas: tuple[tuple[int, int], ...]) -> None:
    try:
        beep = ctypes.windll.kernel32.Beep
        for frequencia, duracao in notas:
            beep(frequencia, duracao)
    except Exception:
        log.debug("não deu para emitir o bipe", exc_info=True)


def tocar(notas: tuple[tuple[int, int], ...]) -> None:
    """Toca sem bloquear quem chamou.

    `Beep` é síncrono: tocado na thread do ditado, atrasaria o início da
    gravação em ~140 ms — tempo suficiente para engolir a primeira sílaba.
    """
    threading.Thread(target=_tocar, args=(notas,), daemon=True).start()
