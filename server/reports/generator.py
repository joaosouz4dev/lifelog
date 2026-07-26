"""Geração e persistência dos relatórios diário e mensal."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path

from .. import db
from ..hub.base import BudgetExceeded, ProviderError
from ..hub.chain import ProviderChain
from .builder import build_day_context, build_day_prompt, fetch_day_segments

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"


class NoMaterial(Exception):
    """Não há transcrição suficiente no período para gerar um relatório."""


def _load_prompt(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"prompt ausente: {path}")
    return path.read_text(encoding="utf-8")


def _save(
    report_type: str, period_start: date, period_end: date, content: str, result
) -> int:
    """Grava o relatório. Regerar o mesmo período substitui o anterior."""
    completion = result.value
    with db.transaction() as conn:
        cursor = conn.execute(
            """
            INSERT INTO reports
                (type, period_start, period_end, content_md, llm_provider,
                 tokens_in, tokens_out, cost_cents)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(type, period_start) DO UPDATE SET
                period_end = excluded.period_end,
                content_md = excluded.content_md,
                llm_provider = excluded.llm_provider,
                tokens_in = excluded.tokens_in,
                tokens_out = excluded.tokens_out,
                cost_cents = excluded.cost_cents,
                generated_at = datetime('now', 'localtime')
            """,
            (
                report_type,
                period_start.isoformat(),
                period_end.isoformat(),
                content,
                result.provider,
                completion.tokens_in,
                completion.tokens_out,
                completion.cost_cents,
            ),
        )
        report_id = cursor.lastrowid

    if not report_id:  # caminho do UPDATE
        row = db.get_connection().execute(
            "SELECT id FROM reports WHERE type = ? AND period_start = ?",
            (report_type, period_start.isoformat()),
        ).fetchone()
        report_id = int(row["id"])

    return int(report_id)


async def generate_daily(chain: ProviderChain, day: date) -> dict:
    """Gera o relatório de um dia. Levanta NoMaterial se não houver fala."""
    rows = fetch_day_segments(db.get_connection(), day)
    if not rows:
        raise NoMaterial(f"nenhuma transcrição em {day.isoformat()}")

    context = build_day_context(rows)
    if not context.text.strip():
        raise NoMaterial(f"transcrições vazias em {day.isoformat()}")

    log.info(
        "relatório de %s: %s trechos, ~%s tokens%s",
        day, context.segment_count, context.estimated_tokens,
        " (truncado)" if context.truncated else "",
    )

    system = _load_prompt("daily")
    prompt = build_day_prompt(day, context)

    result = await chain.run(
        lambda p: p.complete(prompt, system=system),
        cost_estimator=lambda p: p.estimate_cost_cents(context.estimated_tokens, 2000),
    )

    report_id = _save("daily", day, day, result.value.text, result)
    log.info(
        "relatório de %s gerado por %s (%.2f centavos)",
        day, result.provider, result.value.cost_cents,
    )
    return {
        "id": report_id,
        "type": "daily",
        "date": day.isoformat(),
        "provider": result.provider,
        "cost_cents": result.value.cost_cents,
        "truncated": context.truncated,
    }


async def generate_monthly(chain: ProviderChain, year: int, month: int) -> dict:
    """Gera o relatório do mês a partir dos relatórios diários já existentes.

    Consome os diários, não o áudio bruto: um mês de transcrição não caberia
    no contexto, e os diários já filtraram o que importa.
    """
    start = date(year, month, 1)
    end = (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)

    rows = db.get_connection().execute(
        """
        SELECT period_start, content_md FROM reports
         WHERE type = 'daily' AND period_start >= ? AND period_start <= ?
         ORDER BY period_start
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchall()

    if not rows:
        raise NoMaterial(
            f"nenhum relatório diário em {month:02d}/{year} — gere os diários primeiro"
        )

    parts = [
        f"## {date.fromisoformat(r['period_start']).strftime('%d/%m')}\n\n{r['content_md']}"
        for r in rows
    ]
    prompt = (
        f"Mês: {month:02d}/{year}\n"
        f"Relatórios diários disponíveis: {len(rows)}\n\n---\n\n" + "\n\n---\n\n".join(parts)
    )

    system = _load_prompt("monthly")
    estimated_in = len(prompt) // 4

    result = await chain.run(
        lambda p: p.complete(prompt, system=system, max_tokens=8000),
        cost_estimator=lambda p: p.estimate_cost_cents(estimated_in, 4000),
    )

    report_id = _save("monthly", start, end, result.value.text, result)
    log.info(
        "relatório de %02d/%s gerado por %s (%.2f centavos)",
        month, year, result.provider, result.value.cost_cents,
    )
    return {
        "id": report_id,
        "type": "monthly",
        "period": f"{year}-{month:02d}",
        "days_included": len(rows),
        "provider": result.provider,
        "cost_cents": result.value.cost_cents,
    }


__all__ = ["BudgetExceeded", "NoMaterial", "ProviderError",
           "generate_daily", "generate_monthly"]
