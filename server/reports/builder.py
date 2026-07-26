"""Montagem do contexto enviado ao LLM.

Um dia intenso rende ~108 mil tokens de transcrição. Cabe no contexto de 1M,
mas custa caro e dilui o resumo, então o material é compactado antes de subir:
segmentos vizinhos da mesma fonte viram um bloco, e se ainda passar do teto os
trechos mais curtos saem primeiro (interjeições, ruído reconhecido como fala).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from ..classify import classify_app, is_report_worthy

# 1 token ≈ 4 caracteres em português. Aproximação suficiente para decidir
# o corte; o custo real vem dos tokens que a API devolve.
CHARS_PER_TOKEN = 4

SOURCE_LABEL = {"mic": "microfone", "system": "sistema", "gadget": "gadget"}

# Lacuna acima da qual dois segmentos viram blocos separados no contexto.
BLOCK_GAP = timedelta(minutes=3)


@dataclass
class DayContext:
    """Material de um dia, pronto para ir ao LLM."""

    text: str
    segment_count: int
    included_count: int
    speech_ms: int
    estimated_tokens: int
    truncated: bool
    # Quantos segmentos ficaram de fora por serem entretenimento — vira uma
    # nota no relatório para o leitor saber que aquele tempo existiu.
    skipped_entertainment: int = 0
    skipped_speech_ms: int = 0


def _row_app(row: sqlite3.Row) -> str | None:
    """Lê app_name tolerando linhas antigas que não têm a coluna."""
    try:
        return row["app_name"]
    except (IndexError, KeyError):
        return None


def _day_bounds(day: date) -> tuple[str, str]:
    return day.isoformat(), (day + timedelta(days=1)).isoformat()


def fetch_day_segments(conn: sqlite3.Connection, day: date) -> list[sqlite3.Row]:
    """Segmentos transcritos do dia, em ordem cronológica."""
    start, end = _day_bounds(day)
    return conn.execute(
        """
        SELECT s.started_at, s.duration_ms, s.transcript, s.confidence,
               ss.source, COALESCE(s.app_name, ss.app_name) AS app_name
          FROM segments s JOIN sessions ss ON ss.id = s.session_id
         WHERE s.started_at >= ? AND s.started_at < ?
           AND s.status = 'done'
           AND s.transcript IS NOT NULL AND TRIM(s.transcript) != ''
         ORDER BY s.started_at
        """,
        (start, end),
    ).fetchall()


def build_day_context(
    rows: list[sqlite3.Row],
    *,
    max_tokens: int = 120_000,
    skip_entertainment: bool = True,
    include_browser: bool = True,
) -> DayContext:
    """Transforma os segmentos do dia no texto que vai ao LLM.

    Entretenimento é descartado antes de tudo: resumir a série que tocou no
    Netflix não é o relatório do seu dia, e ainda ocupa espaço de contexto que
    a conversa de verdade precisa.
    """
    if not rows:
        return DayContext("", 0, 0, 0, 0, False)

    total_speech = sum(r["duration_ms"] for r in rows)

    kept: list[sqlite3.Row] = []
    skipped_count = 0
    skipped_ms = 0

    for row in rows:
        category = classify_app(_row_app(row), row["source"])
        if skip_entertainment and not is_report_worthy(
            category, include_browser=include_browser
        ):
            skipped_count += 1
            skipped_ms += row["duration_ms"]
            continue
        kept.append(row)

    if not kept:
        return DayContext(
            "", len(rows), 0, total_speech, 0, False, skipped_count, skipped_ms
        )

    budget_chars = max_tokens * CHARS_PER_TOKEN
    selected = kept
    truncated = False

    # Se estourar o teto, descarta os trechos mais curtos primeiro: são
    # interjeições e ruído, e carregam pouca informação por caractere.
    if sum(len(r["transcript"]) for r in selected) > budget_chars:
        truncated = True
        by_length = sorted(selected, key=lambda r: len(r["transcript"]), reverse=True)
        kept, used = [], 0
        for row in by_length:
            cost = len(row["transcript"]) + 40  # +cabeçalho do bloco
            if used + cost > budget_chars:
                continue
            kept.append(row)
            used += cost
        selected = sorted(kept, key=lambda r: r["started_at"])

    # Agrupa segmentos contíguos da mesma fonte num bloco só — o LLM lê melhor
    # um parágrafo do que 400 linhas de uma frase cada.
    lines: list[str] = []
    block: list[str] = []
    block_source: str | None = None
    block_start: datetime | None = None
    previous_end: datetime | None = None

    def flush() -> None:
        if block and block_start is not None:
            label = SOURCE_LABEL.get(block_source or "", block_source or "?")
            lines.append(f"[{block_start:%H:%M}] ({label}) " + " ".join(block))

    for row in selected:
        started = datetime.fromisoformat(row["started_at"])
        gap_too_big = previous_end is not None and started - previous_end > BLOCK_GAP

        if row["source"] != block_source or gap_too_big:
            flush()
            block, block_source, block_start = [], row["source"], started

        block.append(row["transcript"].strip())
        previous_end = started + timedelta(milliseconds=row["duration_ms"])

    flush()

    text = "\n\n".join(lines)
    return DayContext(
        text=text,
        segment_count=len(rows),
        included_count=len(selected),
        speech_ms=total_speech,
        estimated_tokens=len(text) // CHARS_PER_TOKEN,
        truncated=truncated,
        skipped_entertainment=skipped_count,
        skipped_speech_ms=skipped_ms,
    )


def build_day_prompt(day: date, context: DayContext) -> str:
    """Prompt do usuário: cabeçalho factual + a transcrição."""
    header = [
        f"Data: {day.strftime('%d/%m/%Y')} ({_weekday_pt(day)})",
        f"Fala capturada: {context.speech_ms / 60000:.0f} minutos "
        f"em {context.segment_count} trechos",
    ]
    if context.skipped_entertainment:
        header.append(
            f"Nota: {context.skipped_entertainment} trechos "
            f"({context.skipped_speech_ms / 60000:.0f} min) foram identificados como "
            f"série, música ou jogo e não estão abaixo. Não os mencione no relatório; "
            f"esse tempo simplesmente não faz parte dele."
        )
    if context.truncated:
        header.append(
            f"AVISO: o dia não coube inteiro no contexto. "
            f"{context.included_count} dos {context.segment_count} trechos foram "
            f"incluídos — os mais curtos ficaram de fora. Mencione essa limitação "
            f"ao final do relatório."
        )

    return "\n".join(header) + "\n\n---\n\n" + context.text


def _weekday_pt(day: date) -> str:
    return [
        "segunda-feira", "terça-feira", "quarta-feira", "quinta-feira",
        "sexta-feira", "sábado", "domingo",
    ][day.weekday()]
