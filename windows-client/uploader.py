"""Envio dos segmentos enfileirados para o servidor.

Drena a fila continuamente. Erros de rede geram nova tentativa com backoff;
rejeições definitivas do servidor (4xx) descartam o item em vez de entupir
a fila para sempre.
"""

from __future__ import annotations

import json
import logging
import threading
import time

import httpx

from buffer import SegmentQueue

log = logging.getLogger("uploader")

# 4xx que não melhoram com nova tentativa. 429 fica de fora de propósito:
# é limitação temporária e deve ser retentado.
PERMANENT_STATUS = {400, 401, 403, 404, 413, 422}


class Uploader(threading.Thread):
    def __init__(
        self,
        queue: SegmentQueue,
        server_url: str,
        device_id: str,
        *,
        poll_interval: float = 2.0,
        timeout: float = 60.0,
    ):
        super().__init__(name="uploader", daemon=True)
        self.queue = queue
        self.server_url = server_url.rstrip("/")
        self.device_id = device_id
        self.poll_interval = poll_interval
        self.timeout = timeout
        self._stop = threading.Event()
        self.sent_count = 0
        self._offline_since: float | None = None

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        log.info("uploader apontando para %s", self.server_url)
        with httpx.Client(timeout=self.timeout) as client:
            while not self._stop.is_set():
                try:
                    batch = self.queue.next_batch(limit=5)
                except Exception:
                    log.exception("falha ao ler a fila")
                    self._stop.wait(self.poll_interval)
                    continue

                if not batch:
                    self._stop.wait(self.poll_interval)
                    continue

                for segment in batch:
                    if self._stop.is_set():
                        break
                    self._send(client, segment)

    def _send(self, client: httpx.Client, segment) -> None:
        if not segment.audio_path.exists():
            self.queue.drop_permanently_rejected(segment, "arquivo de áudio sumiu do disco")
            return

        meta = {
            "device_id": self.device_id,
            "source": segment.source,
            "started_at": segment.started_at.isoformat(),
            "duration_ms": segment.duration_ms,
            "client_uid": segment.client_uid,
        }
        if segment.app_name:
            meta["app_name"] = segment.app_name

        try:
            response = client.post(
                f"{self.server_url}/ingest",
                files={
                    "audio": (
                        segment.audio_path.name,
                        segment.audio_path.read_bytes(),
                        "audio/ogg",
                    )
                },
                data={"meta": json.dumps(meta)},
            )
        except httpx.HTTPError as exc:
            if self._offline_since is None:
                self._offline_since = time.time()
                log.warning("servidor inacessível, acumulando na fila local: %s", exc)
            self.queue.mark_failed(segment, str(exc))
            return

        if response.status_code == 200:
            if self._offline_since is not None:
                offline_for = time.time() - self._offline_since
                log.info("servidor de volta após %.0fs; drenando a fila", offline_for)
                self._offline_since = None
            self.queue.mark_sent(segment)
            self.sent_count += 1
            body = response.json()
            log.info(
                "enviado %s -> segmento %s%s",
                segment.client_uid,
                body.get("segment_id"),
                " (já existia)" if body.get("duplicate") else "",
            )
            return

        detail = f"HTTP {response.status_code}: {response.text[:200]}"
        if response.status_code in PERMANENT_STATUS:
            self.queue.drop_permanently_rejected(segment, detail)
        else:
            self.queue.mark_failed(segment, detail)
            log.warning("envio falhou (%s), tentará de novo", detail)
