"""Modelos de dados compartilhados entre clientes e servidor.

Estes tipos são o contrato de ingestão. Windows, Android e o gadget futuro
falam todos este mesmo formato — ver protocol/ingest.md.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class Source(StrEnum):
    MIC = "mic"
    SYSTEM = "system"
    GADGET = "gadget"


class SegmentStatus(StrEnum):
    PENDING = "pending"
    TRANSCRIBING = "transcribing"
    DONE = "done"
    FAILED = "failed"


class IngestMeta(BaseModel):
    """Metadados que acompanham o áudio no multipart de /ingest."""

    device_id: str = Field(min_length=1, max_length=64)
    source: Source
    # ISO 8601. Clientes devem enviar com offset (ex.: 2026-07-25T14:32:07-03:00);
    # sem offset assume-se a hora local do servidor — ver o validador abaixo.
    started_at: datetime
    duration_ms: int = Field(gt=0, le=600_000)
    # Identificador estável gerado pelo cliente. Reenviar o mesmo uid após um
    # timeout não cria um segmento duplicado.
    client_uid: str = Field(min_length=8, max_length=64)
    app_name: str | None = Field(default=None, max_length=200)
    sample_rate: int = Field(default=16000, ge=8000, le=48000)

    @field_validator("started_at")
    @classmethod
    def normalize_timezone(cls, value: datetime) -> datetime:
        """Converte para a hora local do servidor e remove o offset.

        Dispositivos em fusos diferentes (celular viajando, gadget com relógio
        próprio) alimentam o mesmo banco. Sem normalizar, `date(started_at)`
        agruparia o mesmo instante em dias diferentes conforme a origem.

        Timestamps sem offset são aceitos como já sendo hora local — é o que
        os clientes atuais enviam.
        """
        if value.tzinfo is None:
            return value
        return value.astimezone().replace(tzinfo=None)


class IngestResponse(BaseModel):
    segment_id: int
    status: SegmentStatus
    duplicate: bool = False


class Segment(BaseModel):
    id: int
    session_id: int
    source: Source
    started_at: datetime
    duration_ms: int
    transcript: str | None = None
    language: str | None = None
    confidence: float | None = None
    speaker_label: str | None = None
    stt_provider: str | None = None
    status: SegmentStatus
    app_name: str | None = None
    has_audio: bool = False


class Transcript(BaseModel):
    """Resultado devolvido por um provedor de STT."""

    text: str
    language: str | None = None
    confidence: float | None = None
    provider: str = ""
    cost_cents: float = 0.0


class ProviderHealth(BaseModel):
    name: str
    available: bool
    circuit_open: bool
    consecutive_failures: int
    detail: str | None = None


class HubStatus(BaseModel):
    hub: str
    chain: list[str]
    providers: list[ProviderHealth]
    spent_today_cents: float
    daily_budget_cents: float


class DayStats(BaseModel):
    date: str
    total_segments: int
    total_speech_ms: int
    pending: int
    failed: int
    by_source: dict[str, int]
