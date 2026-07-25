"""Worker de transcrição.

Consome segmentos pendentes e os manda para o hub de STT. Roda como task
asyncio dentro do servidor — um único worker, porque o gargalo é a GPU e
paralelizar aqui só causaria disputa de VRAM.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .. import db
from ..hub.base import BudgetExceeded, ProviderError
from ..hub.chain import ProviderChain

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
IDLE_SLEEP = 2.0        # espera quando a fila está vazia
BACKOFF_SLEEP = 30.0    # espera após falha de todos os provedores


class TranscriptionWorker:
    def __init__(self, chain: ProviderChain, *, language: str = "pt"):
        self.chain = chain
        self.language = language
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._loop(), name="transcription-worker")
            log.info("worker de transcrição iniciado")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            log.info("worker de transcrição parado")

    # ──────────────────────────────── loop ────────────────────────────────

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                segment = await asyncio.to_thread(self._claim_next)
            except Exception:
                log.exception("erro ao buscar segmento pendente")
                await asyncio.sleep(BACKOFF_SLEEP)
                continue

            if segment is None:
                await asyncio.sleep(IDLE_SLEEP)
                continue

            await self._process(segment)

    def _claim_next(self) -> dict | None:
        """Marca o próximo pendente como 'transcribing' e o devolve.

        Um UPDATE condicional evita que uma reentrada pegue o mesmo segmento.
        """
        with db.transaction() as conn:
            row = conn.execute(
                """
                SELECT id, audio_path, attempts, duration_ms FROM segments
                 WHERE status = 'pending' AND attempts < ?
                 ORDER BY started_at LIMIT 1
                """,
                (MAX_ATTEMPTS,),
            ).fetchone()
            if row is None:
                return None

            updated = conn.execute(
                """
                UPDATE segments SET status = 'transcribing', attempts = attempts + 1
                 WHERE id = ? AND status = 'pending'
                """,
                (row["id"],),
            )
            if updated.rowcount == 0:
                return None
            return {
                "id": int(row["id"]),
                "audio_path": row["audio_path"],
                "attempts": int(row["attempts"]) + 1,
                "duration_ms": int(row["duration_ms"]),
            }

    async def _process(self, segment: dict) -> None:
        segment_id = segment["id"]
        audio_path = Path(segment["audio_path"]) if segment["audio_path"] else None

        if audio_path is None or not audio_path.exists():
            await asyncio.to_thread(
                self._mark_failed, segment_id, "arquivo de áudio ausente", final=True
            )
            return

        # duration_ms já veio do SELECT que reservou o segmento; o estimador é
        # chamado uma vez por provedor e não deve tocar o banco por isso.
        duration_ms = segment["duration_ms"]
        try:
            result = await self.chain.run(
                lambda p: p.transcribe(audio_path, self.language),
                cost_estimator=lambda p: p.estimate_cost_cents(duration_ms),
            )
        except BudgetExceeded as exc:
            # Não é culpa do segmento: devolve para a fila sem gastar tentativa.
            log.warning("teto de gasto atingido; segmento %s volta para a fila", segment_id)
            await asyncio.to_thread(self._requeue, segment_id, str(exc))
            await asyncio.sleep(BACKOFF_SLEEP)
            return
        except ProviderError as exc:
            final = segment["attempts"] >= MAX_ATTEMPTS or not getattr(exc, "retryable", True)
            await asyncio.to_thread(self._mark_failed, segment_id, str(exc), final=final)
            if not final:
                await asyncio.sleep(BACKOFF_SLEEP)
            return
        except Exception as exc:
            log.exception("erro inesperado transcrevendo o segmento %s", segment_id)
            final = segment["attempts"] >= MAX_ATTEMPTS
            await asyncio.to_thread(self._mark_failed, segment_id, repr(exc), final=final)
            return

        await asyncio.to_thread(self._mark_done, segment_id, result)
        log.info(
            "segmento %s transcrito por %s em %sms: %r",
            segment_id, result.provider, result.latency_ms, result.value.text[:60],
        )

    # ──────────────────────────── persistência ────────────────────────────

    @staticmethod
    def _mark_done(segment_id: int, result) -> None:
        transcript = result.value
        with db.transaction() as conn:
            conn.execute(
                """
                UPDATE segments
                   SET status = 'done', transcript = ?, language = ?, confidence = ?,
                       stt_provider = ?, cost_cents = ?, error = NULL,
                       transcribed_at = datetime('now')
                 WHERE id = ?
                """,
                (
                    transcript.text,
                    transcript.language,
                    transcript.confidence,
                    result.provider,
                    result.cost_cents,
                    segment_id,
                ),
            )

    @staticmethod
    def _mark_failed(segment_id: int, error: str, *, final: bool) -> None:
        with db.transaction() as conn:
            conn.execute(
                "UPDATE segments SET status = ?, error = ? WHERE id = ?",
                ("failed" if final else "pending", error[:500], segment_id),
            )
        if final:
            log.error("segmento %s falhou definitivamente: %s", segment_id, error[:200])

    @staticmethod
    def _requeue(segment_id: int, reason: str) -> None:
        """Devolve à fila sem consumir tentativa (falha externa, não do segmento)."""
        with db.transaction() as conn:
            conn.execute(
                """
                UPDATE segments
                   SET status = 'pending', error = ?, attempts = MAX(attempts - 1, 0)
                 WHERE id = ?
                """,
                (reason[:500], segment_id),
            )


def retry_failed() -> int:
    """Recoloca segmentos falhados na fila. Exposto na UI como 'tentar de novo'."""
    with db.transaction() as conn:
        cursor = conn.execute(
            "UPDATE segments SET status = 'pending', attempts = 0, error = NULL "
            "WHERE status = 'failed'"
        )
    return cursor.rowcount
