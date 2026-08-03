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
    # Capturado e guardado, mas fora da lista de permitidos: aparece na
    # timeline com a origem à vista, sem consumir transcrição.
    SKIPPED = "skipped"


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
    # Categoria derivada do app: conversa, entretenimento, navegador…
    # É o que permite à interface marcar o que ficou fora do relatório.
    category: str | None = None
    has_audio: bool = False


class Transcript(BaseModel):
    """Resultado devolvido por um provedor de STT."""

    text: str
    language: str | None = None
    confidence: float | None = None
    provider: str = ""
    cost_cents: float = 0.0


class Completion(BaseModel):
    """Resultado devolvido por um provedor de LLM.

    Espelha Transcript: o hub lê `cost_cents` para o teto de gasto, então todo
    provedor precisa devolver o custo já calculado.
    """

    text: str
    provider: str = ""
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    cost_cents: float = 0.0
    # 'refusal' quando o modelo recusou por segurança — o texto vem vazio e
    # não adianta repetir o mesmo pedido.
    stop_reason: str | None = None


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
    # Capturados mas fora da lista de permitidos — nunca transcritos.
    skipped: int = 0
    by_source: dict[str, int]


# Um termo vazio casaria com qualquer coisa (`"" in alvo` é sempre True) e
# transformaria a allowlist em "permite tudo" sem ninguém perceber.
MAX_TERMOS = 200
MAX_TAMANHO_TERMO = 200


def _limpar_termos(termos: list[str]) -> list[str]:
    vistos: list[str] = []
    for termo in termos:
        limpo = termo.strip().lower()
        if limpo and limpo not in vistos:
            vistos.append(limpo[:MAX_TAMANHO_TERMO])
    return vistos[:MAX_TERMOS]


class CaptureConfig(BaseModel):
    """O que deve ser transcrito, editável pela interface.

    Casa por pedaço de texto tanto no nome do programa quanto no título da
    janela — é o título que revela o site aberto, já que no navegador tudo é
    "chrome.exe".
    """

    allowlist: list[str] = []
    blocklist: list[str] = []

    @field_validator("allowlist", "blocklist")
    @classmethod
    def normalizar(cls, termos: list[str]) -> list[str]:
        return _limpar_termos(termos)
