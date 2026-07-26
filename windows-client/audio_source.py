"""Descobre qual programa está emitindo som no Windows.

O WASAPI loopback captura a mistura final da placa de som — não diz de onde
veio cada trecho. Sem isso, uma série no Netflix entra no relatório do dia
junto com a reunião de trabalho.

A API de sessões de áudio do Windows resolve: lista os processos com áudio
ativo e o pico de volume de cada um no instante da consulta.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

log = logging.getLogger("audio_source")

# Abaixo disto o app tem sessão aberta mas está em silêncio — um player
# pausado, ou um navegador com aba muda.
SILENCE_THRESHOLD = 0.0015

# Consultar o COM a cada leitura de 100ms seria desperdício; o app que toca
# não muda nessa escala.
CACHE_SECONDS = 1.5

# O próprio capturador aparece na lista de sessões de áudio porque tem o
# loopback aberto. Rotular um segmento com o nome do Lifelog não diz nada
# sobre a origem — e no app empacotado os nomes mudam para Lifelog.exe.
SELF_PROCESSES = frozenset({
    "python.exe",
    "pythonw.exe",
    "lifelog.exe",
    "lifelogserver.exe",
})


@dataclass(frozen=True)
class ActiveApp:
    process: str   # "msedge.exe"
    peak: float    # 0.0 a 1.0


class AudioSourceProbe:
    """Consulta os apps que estão emitindo som, com cache curto.

    Degrada em silêncio: se o pycaw faltar ou o COM falhar, devolve lista
    vazia e a captura segue sem marcar a origem — perder o rótulo é bem menos
    grave que perder o áudio.
    """

    def __init__(self, cache_seconds: float = CACHE_SECONDS):
        self.cache_seconds = cache_seconds
        self._lock = threading.Lock()
        self._cached: list[ActiveApp] = []
        self._cached_at = 0.0
        self._available: bool | None = None

    def _probe(self) -> list[ActiveApp]:
        try:
            from pycaw.pycaw import AudioUtilities, IAudioMeterInformation
        except ImportError:
            if self._available is not False:
                log.info("pycaw indisponível — a origem do áudio não será registrada")
            self._available = False
            return []

        # Cada thread que usa COM precisa inicializá-lo. A captura roda em
        # threads próprias, então isso não é opcional.
        try:
            import comtypes

            comtypes.CoInitialize()
        except Exception:
            pass

        found: list[ActiveApp] = []
        try:
            for session in AudioUtilities.GetAllSessions():
                if not session.Process:
                    continue
                try:
                    meter = session._ctl.QueryInterface(IAudioMeterInformation)
                    peak = float(meter.GetPeakValue())
                except Exception:
                    continue
                if peak >= SILENCE_THRESHOLD:
                    found.append(ActiveApp(session.Process.name().lower(), peak))
        except Exception:
            log.debug("falha ao consultar as sessões de áudio", exc_info=True)
            return []

        self._available = True
        return sorted(found, key=lambda a: a.peak, reverse=True)

    def active_apps(self) -> list[ActiveApp]:
        """Apps emitindo som agora, do mais alto para o mais baixo."""
        with self._lock:
            now = time.monotonic()
            if now - self._cached_at < self.cache_seconds:
                return self._cached
            self._cached = self._probe()
            self._cached_at = now
            return self._cached

    def current_app_name(self, *, exclude_self: bool = True) -> str | None:
        """Nome do app dominante, ou None se nada estiver tocando.

        `exclude_self` descarta o próprio processo de captura: o Python que
        lê o loopback aparece na lista e não é a fonte real do som.
        """
        apps = self.active_apps()
        if exclude_self:
            apps = [a for a in apps if a.process not in SELF_PROCESSES]
        return apps[0].process if apps else None

    def snapshot(self) -> str | None:
        """Rótulo compacto para gravar no segmento.

        Vários apps ao mesmo tempo viram "teams.exe+spotify.exe" — a
        classificação depois decide o que fazer com a combinação.
        """
        apps = [
            a for a in self.active_apps() if a.process not in SELF_PROCESSES
        ]
        if not apps:
            return None
        return "+".join(a.process for a in apps[:3])
