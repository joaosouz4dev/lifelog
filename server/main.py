"""Servidor Lifelog — ingestão, consulta e UI.

Sobe com:  uvicorn server.main:app --reload
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import chat, db, reports, version
from .classify import classify_app
from .config import get_config
from .hub.base import BudgetExceeded, ProviderError
from .hub.llm import build_llm_chain
from .hub.stt import build_stt_chain
from .models import DayStats, HubStatus, IngestMeta, IngestResponse, Segment, Source
from .pipeline import ingest as ingest_pipeline
from .pipeline.transcribe import TranscriptionWorker, retry_failed

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("lifelog")

cfg = get_config()
DATA_DIR = cfg.resolve_path("server.data_dir", "./data")
DB_PATH = cfg.resolve_path("server.db_path", "./data/lifelog.db")
WEB_DIR = Path(__file__).parent / "web"

MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB — muito acima de um chunk de 30s

worker: TranscriptionWorker | None = None
stt_chain = None
llm_chain = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global worker, stt_chain, llm_chain

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db.init(DB_PATH)
    log.info("banco pronto em %s", DB_PATH)

    stt_chain = build_stt_chain(cfg)
    log.info("cadeia de STT: %s", " -> ".join(stt_chain.chain_names) or "(vazia)")

    llm_chain = build_llm_chain(cfg)
    log.info("cadeia de LLM: %s", " -> ".join(llm_chain.chain_names) or "(vazia)")

    # Idioma no nível do hub: alcançar dentro da config de um provedor nomeado
    # faria trocar a ordem da cadeia mudar silenciosamente o idioma.
    worker = TranscriptionWorker(stt_chain, language=cfg.get("stt.language", "pt"))
    worker.start()

    yield

    if worker is not None:
        await worker.stop()
    db.close_thread_connection()


app = FastAPI(title="Lifelog", version="0.1.0", lifespan=lifespan)


# ─────────────────────────────── ingestão ───────────────────────────────


@app.post("/ingest", response_model=IngestResponse)
async def ingest(
    audio: UploadFile = File(...),
    meta: str = Form(...),
) -> IngestResponse:
    """Recebe um segmento de fala. Contrato completo em protocol/ingest.md.

    multipart/form-data com dois campos: `audio` (Opus) e `meta` (JSON).
    """
    try:
        parsed = IngestMeta.model_validate_json(meta)
    except Exception as exc:
        raise HTTPException(422, f"meta inválido: {exc}") from exc

    payload = await audio.read()
    if not payload:
        raise HTTPException(422, "áudio vazio")
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"áudio maior que {MAX_UPLOAD_BYTES} bytes")

    try:
        ingest_pipeline.validate_audio(payload)
    except ingest_pipeline.InvalidAudio as exc:
        # 422 é definitivo: o cliente descarta em vez de insistir com o mesmo
        # payload inválido para sempre.
        log.warning("áudio rejeitado de %s: %s", parsed.device_id, exc)
        raise HTTPException(422, str(exc)) from exc

    try:
        return ingest_pipeline.ingest_segment(DATA_DIR, parsed, payload)
    except Exception as exc:
        log.exception("falha ao ingerir %s", parsed.client_uid)
        raise HTTPException(500, f"falha ao gravar o segmento: {exc}") from exc


# ──────────────────────────────── consulta ────────────────────────────────


def _day_bounds(day: str | None) -> tuple[str, str, str]:
    """Devolve (dia, início, fim_exclusivo) para filtrar por intervalo.

    Filtrar com `date(started_at) = ?` envolve a coluna numa função e o SQLite
    descarta o índice, caindo em full scan (~21 ms com um ano de dados, contra
    0,04 ms por intervalo). Como ISO 8601 ordena lexicograficamente, um simples
    `>= início AND < fim` usa idx_segments_started direto.
    """
    target = day or date.today().isoformat()
    start = datetime.fromisoformat(target).date()
    return target, start.isoformat(), (start + timedelta(days=1)).isoformat()


def _row_to_segment(row) -> Segment:
    app_name = row["app_name"]
    return Segment(
        id=row["id"],
        session_id=row["session_id"],
        source=Source(row["source"]),
        started_at=datetime.fromisoformat(row["started_at"]),
        duration_ms=row["duration_ms"],
        transcript=row["transcript"],
        language=row["language"],
        confidence=row["confidence"],
        speaker_label=row["speaker_label"],
        stt_provider=row["stt_provider"],
        status=row["status"],
        app_name=app_name,
        category=classify_app(app_name, row["source"]).value,
        has_audio=bool(row["audio_path"]),
    )


@app.get("/api/segments", response_model=list[Segment])
def list_segments(
    day: str | None = Query(None, description="YYYY-MM-DD (padrão: hoje)"),
    source: Source | None = None,
    limit: int = Query(500, le=2000),
) -> list[Segment]:
    _, start, end = _day_bounds(day)
    sql = """
        SELECT s.*, ss.source, COALESCE(s.app_name, ss.app_name) AS app_name
          FROM segments s JOIN sessions ss ON ss.id = s.session_id
         WHERE s.started_at >= ? AND s.started_at < ?
    """
    params: list = [start, end]
    if source is not None:
        sql += " AND ss.source = ?"
        params.append(source.value)
    sql += " ORDER BY s.started_at LIMIT ?"
    params.append(limit)

    rows = db.get_connection().execute(sql, params).fetchall()
    return [_row_to_segment(r) for r in rows]


@app.get("/api/search", response_model=list[Segment])
def search(q: str = Query(min_length=2), limit: int = Query(100, le=500)) -> list[Segment]:
    """Busca textual (FTS5, insensível a acento). Semântica chega na Fase 3."""
    rows = db.get_connection().execute(
        """
        SELECT s.*, ss.source, COALESCE(s.app_name, ss.app_name) AS app_name
          FROM segments_fts f
          JOIN segments s  ON s.id = f.rowid
          JOIN sessions ss ON ss.id = s.session_id
         WHERE segments_fts MATCH ?
         ORDER BY rank LIMIT ?
        """,
        (q, limit),
    ).fetchall()
    return [_row_to_segment(r) for r in rows]


@app.get("/api/segments/{segment_id}/audio")
def get_audio(segment_id: int) -> FileResponse:
    row = db.get_connection().execute(
        "SELECT audio_path FROM segments WHERE id = ?", (segment_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "segmento não encontrado")
    if not row["audio_path"]:
        raise HTTPException(410, "áudio removido pela política de retenção")

    path = Path(row["audio_path"])
    if not path.exists():
        raise HTTPException(410, "arquivo de áudio ausente no disco")
    return FileResponse(path, media_type="audio/ogg", filename=path.name)


@app.get("/api/stats", response_model=DayStats)
def stats(day: str | None = None) -> DayStats:
    target, start, end = _day_bounds(day)

    # Um único agrupamento por fonte cobre totais e distribuição: antes eram
    # duas varreduras do mesmo intervalo, e a UI chama isto a cada 4s.
    rows = db.get_connection().execute(
        """
        SELECT ss.source AS source,
               COUNT(*) AS n,
               COALESCE(SUM(s.duration_ms), 0) AS speech_ms,
               COALESCE(SUM(s.status IN ('pending', 'transcribing')), 0) AS pending,
               COALESCE(SUM(s.status = 'failed'), 0) AS failed
          FROM segments s JOIN sessions ss ON ss.id = s.session_id
         WHERE s.started_at >= ? AND s.started_at < ?
         GROUP BY ss.source
        """,
        (start, end),
    ).fetchall()

    return DayStats(
        date=target,
        total_segments=sum(r["n"] for r in rows),
        total_speech_ms=sum(r["speech_ms"] for r in rows),
        pending=sum(r["pending"] for r in rows),
        failed=sum(r["failed"] for r in rows),
        by_source={r["source"]: r["n"] for r in rows},
    )


@app.get("/api/days")
def list_days(limit: int = Query(60, le=365)) -> list[dict]:
    """Dias com captura — alimenta o seletor de data da UI."""
    rows = db.get_connection().execute(
        """
        SELECT date(started_at) AS day, COUNT(*) AS n
          FROM segments GROUP BY day ORDER BY day DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [{"day": r["day"], "segments": r["n"]} for r in rows]


# ──────────────────────────────── operação ────────────────────────────────


@app.get("/api/hub/stt", response_model=HubStatus)
async def hub_status() -> HubStatus:
    if stt_chain is None:
        raise HTTPException(503, "hub não inicializado")
    return HubStatus(
        hub="stt",
        chain=stt_chain.chain_names,
        providers=await stt_chain.health(),
        spent_today_cents=stt_chain.spent_today_cents(),
        daily_budget_cents=stt_chain.daily_budget_cents,
    )


@app.get("/api/hub/llm", response_model=HubStatus)
async def llm_status() -> HubStatus:
    if llm_chain is None:
        raise HTTPException(503, "hub não inicializado")
    return HubStatus(
        hub="llm",
        chain=llm_chain.chain_names,
        providers=await llm_chain.health(),
        spent_today_cents=llm_chain.spent_today_cents(),
        daily_budget_cents=llm_chain.daily_budget_cents,
    )


# ─────────────────────────────── relatórios ───────────────────────────────


@app.get("/api/reports")
def list_reports(limit: int = Query(60, le=365)) -> list[dict]:
    rows = db.get_connection().execute(
        """
        SELECT id, type, period_start, period_end, llm_provider, cost_cents, generated_at
          FROM reports ORDER BY period_start DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/reports/{report_id}")
def get_report(report_id: int) -> dict:
    row = db.get_connection().execute(
        "SELECT * FROM reports WHERE id = ?", (report_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "relatório não encontrado")
    return dict(row)


@app.post("/api/reports/daily")
async def create_daily_report(day: str | None = None) -> dict:
    """Gera (ou regera) o relatório de um dia. Padrão: ontem."""
    if llm_chain is None:
        raise HTTPException(503, "hub de LLM não inicializado")

    target = date.fromisoformat(day) if day else date.today() - timedelta(days=1)
    try:
        return await reports.generate_daily(llm_chain, target)
    except reports.NoMaterial as exc:
        raise HTTPException(404, str(exc)) from exc
    except BudgetExceeded as exc:
        raise HTTPException(429, str(exc)) from exc
    except ProviderError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/reports/monthly")
async def create_monthly_report(month: str | None = None) -> dict:
    """Gera o relatório de um mês (YYYY-MM). Padrão: o mês passado."""
    if llm_chain is None:
        raise HTTPException(503, "hub de LLM não inicializado")

    if month:
        year, mon = (int(p) for p in month.split("-", 1))
    else:
        first_of_month = date.today().replace(day=1)
        previous = first_of_month - timedelta(days=1)
        year, mon = previous.year, previous.month

    try:
        return await reports.generate_monthly(llm_chain, year, mon)
    except reports.NoMaterial as exc:
        raise HTTPException(404, str(exc)) from exc
    except BudgetExceeded as exc:
        raise HTTPException(429, str(exc)) from exc
    except ProviderError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/chat")
async def chat_endpoint(body: dict) -> dict:
    """Responde uma pergunta sobre o que foi dito, citando as fontes."""
    question = (body.get("question") or "").strip()
    if not question:
        raise HTTPException(422, "pergunta vazia")
    if llm_chain is None:
        raise HTTPException(503, "hub de LLM não inicializado")

    try:
        return await chat.answer(llm_chain, question)
    except BudgetExceeded as exc:
        raise HTTPException(429, str(exc)) from exc
    except ProviderError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/segments/retry")
def retry_all() -> dict:
    return {"requeued": retry_failed()}


@app.post("/api/retention/purge")
def purge() -> dict:
    days = int(cfg.get("retention.audio_days", 30) or 0)
    return {"removed": ingest_pipeline.purge_old_audio(DATA_DIR, days), "retention_days": days}


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "version": version.current()}


@app.get("/api/version")
async def version_info() -> dict:
    """Versão em execução e se há atualização publicada."""
    return await version.check_update()


# ──────────────────────────────────  UI  ──────────────────────────────────

if (WEB_DIR / "static").exists():
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    page = WEB_DIR / "index.html"
    if not page.exists():
        return HTMLResponse("<h1>Lifelog</h1><p>UI não encontrada.</p>", status_code=200)
    return HTMLResponse(page.read_text(encoding="utf-8"))
