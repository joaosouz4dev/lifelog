"""Ditado: segure a tecla, fale, solte, e o texto vai para o campo focado.

O áudio não vem de um dispositivo próprio — vem de um desvio na trilha do
microfone que já está aberta. Abrir um segundo stream causaria o segfault
documentado em `capture.py`, e ainda gastaria o dobro de CPU.

Sem VAD de propósito: os limites da fala são as bordas da tecla. Passar pelo
VAD descartaria um "sim" (min_speech_ms) e cortaria no meio de uma pausa
natural (min_silence_ms).
"""

from __future__ import annotations

import logging
import threading
import time

import numpy as np

log = logging.getLogger("dictation")

SAMPLE_RATE = 16000

# Uma tecla travada não pode virar uma transcrição de meia hora. O servidor
# também recusa, mas cortar aqui evita mandar megabytes à toa.
MAX_SEGUNDOS = 120

# Abaixo disto não houve fala — apertar a tecla sem querer não deve bater no
# servidor nem sobrescrever o que estava no campo.
MIN_SEGUNDOS = 0.25


class DictationTap:
    """Buffer que a trilha do microfone alimenta enquanto o ditado grava.

    Precisa ser barato: roda dentro do laço de captura, e qualquer bloqueio
    aqui atrasa `stream.read` e estoura o buffer do driver.
    """

    def __init__(self, max_segundos: float = MAX_SEGUNDOS):
        self.max_amostras = int(max_segundos * SAMPLE_RATE)
        self._ativo = threading.Event()
        self._lock = threading.Lock()
        self._pedacos: list[np.ndarray] = []
        self._amostras = 0

    @property
    def ativo(self) -> bool:
        return self._ativo.is_set()

    def comecar(self) -> None:
        with self._lock:
            self._pedacos.clear()
            self._amostras = 0
        self._ativo.set()

    def alimentar(self, chunk: np.ndarray) -> None:
        """Chamado pela trilha a cada 100 ms de áudio."""
        with self._lock:
            if self._amostras >= self.max_amostras:
                return  # teto atingido: para de acumular, mas segue drenando
            self._pedacos.append(chunk)
            self._amostras += chunk.size

    def terminar(self) -> np.ndarray | None:
        """Fecha a gravação e devolve o áudio, ou None se não houve fala."""
        self._ativo.clear()
        with self._lock:
            pedacos, self._pedacos = self._pedacos, []
            amostras, self._amostras = self._amostras, 0

        if amostras < MIN_SEGUNDOS * SAMPLE_RATE:
            return None
        return np.concatenate(pedacos)

    def cancelar(self) -> None:
        self._ativo.clear()
        with self._lock:
            self._pedacos.clear()
            self._amostras = 0


class DictationController:
    """Amarra a tecla, o buffer, a transcrição e a digitação."""

    def __init__(self, server_url: str, tap: DictationTap, *, bitrate: int = 24000):
        self.server_url = server_url.rstrip("/")
        self.tap = tap
        self.bitrate = bitrate
        self.ultimo_texto: str | None = None
        self.ultimo_erro: str | None = None
        self.gravando = False
        self._lock = threading.Lock()
        self._alvo_no_inicio = None

    # ──────────────────────────── ciclo da tecla ────────────────────────────

    def on_press(self) -> None:
        with self._lock:
            if self.gravando:
                return
            self.gravando = True

        # Guarda quem estava em foco: se a pessoa trocar de janela no meio da
        # fala, digitar no destino errado pode ser desastroso.
        self._alvo_no_inicio = _janela_em_foco()
        self.ultimo_erro = None
        self.tap.comecar()
        log.info("ditado: gravando…")

        # O modelo pode estar descarregado (~28s para voltar). Aquecer agora,
        # em paralelo à fala, faz esse custo desaparecer.
        threading.Thread(target=self._aquecer, daemon=True).start()

    def on_release(self) -> None:
        with self._lock:
            if not self.gravando:
                return
            self.gravando = False

        audio = self.tap.terminar()
        if audio is None:
            log.info("ditado: nada falado")
            return

        threading.Thread(target=self._processar, args=(audio,), daemon=True).start()

    def cancelar(self) -> None:
        """Esc no meio da fala: joga fora sem transcrever."""
        with self._lock:
            self.gravando = False
        self.tap.cancelar()
        log.info("ditado: cancelado")

    # ──────────────────────────── bastidores ────────────────────────────

    def _aquecer(self) -> None:
        try:
            import httpx

            httpx.post(f"{self.server_url}/api/dictate/aquecer", timeout=120)
        except Exception:
            log.debug("falha ao aquecer o modelo", exc_info=True)

    def _processar(self, audio: np.ndarray) -> None:
        inicio = time.monotonic()
        try:
            texto = self._transcrever(audio)
        except Exception as exc:
            self.ultimo_erro = str(exc)
            log.warning("ditado: falha ao transcrever — %s", exc)
            return

        if not texto:
            self.ultimo_erro = "nada reconhecido"
            log.info("ditado: transcrição vazia")
            return

        self.ultimo_texto = texto
        log.info("ditado: %r (%.1fs)", texto[:60], time.monotonic() - inicio)
        self._entregar(texto)

    def _transcrever(self, audio: np.ndarray) -> str:
        import httpx

        from capture import encode_opus

        payload = encode_opus(audio, self.bitrate)
        resposta = httpx.post(
            f"{self.server_url}/api/dictate",
            files={"audio": ("ditado.opus", payload, "audio/ogg")},
            timeout=180,
        )
        if resposta.status_code != 200:
            detalhe = resposta.json().get("detail", resposta.text[:120])
            raise RuntimeError(detalhe)
        return (resposta.json().get("text") or "").strip()

    def _entregar(self, texto: str) -> None:
        """Digita no campo focado, ou deixa no clipboard se o alvo mudou."""
        try:
            from typer import copiar, digitar
        except ImportError:
            log.warning("digitação indisponível neste sistema")
            return

        alvo_agora = _janela_em_foco()
        if self._alvo_no_inicio is not None and alvo_agora != self._alvo_no_inicio:
            # Digitar aqui mandaria o texto para a janela errada — num
            # terminal, isso pode executar comandos.
            copiar(texto)
            self.ultimo_erro = "janela mudou — texto copiado"
            log.info("ditado: a janela mudou; texto no clipboard")
            return

        digitar(texto)


def _janela_em_foco():
    """Handle da janela em primeiro plano, ou None fora do Windows."""
    try:
        import ctypes

        return ctypes.windll.user32.GetForegroundWindow()
    except Exception:
        return None
