"""Chat sobre as transcrições.

Responde perguntas sobre o que foi dito, com base no que está gravado.
Busca os trechos relevantes, monta o contexto e passa ao hub de LLM.

Cada resposta cita os trechos que a sustentam — sem isso não haveria como
distinguir uma resposta apoiada no histórico de uma inventada.

A busca é textual (FTS5). Semântica exigiria embeddings e um modelo extra;
o FTS já ignora acentos e conjuga bem em português, o que resolve a maioria
das perguntas.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from . import db
from .classify import Category, classify_app, is_report_worthy
from .hub.chain import ProviderChain

log = logging.getLogger(__name__)

# Palavras curtas e comuns só trazem ruído para o FTS.
STOPWORDS = frozenset("""
a o e de da do das dos em no na nos nas um uma uns umas que qual quais
para por com sem sobre como quando onde quem eu me meu minha mim voce
ele ela nos eles elas isso isto aquele aquela ser estar ter foi era
sao esta estao muito mais menos ja nao sim entao pra pro ao aos
""".split())

MAX_SEGMENTS = 60
SNIPPET_CHARS = 400


@dataclass
class Source:
    """Um trecho que embasa a resposta."""

    segment_id: int
    started_at: str
    source: str
    text: str


def _extract_terms(question: str) -> list[str]:
    """Termos de busca a partir da pergunta.

    Quem pergunta escreve em linguagem natural ("o que eu falei sobre o
    orçamento?"), mas o FTS precisa das palavras que importam.
    """
    words = re.findall(r"\w+", question.lower(), re.UNICODE)
    return [w for w in words if len(w) > 3 and w not in STOPWORDS]


def _parse_period(question: str) -> tuple[str, str] | None:
    """Recorte temporal mencionado na pergunta, se houver.

    Não é interpretação de linguagem natural completa — só os casos comuns
    ("ontem", "esta semana"), que mudam bastante o resultado da busca.
    """
    q = question.lower()
    today = date.today()

    if "ontem" in q:
        day = today - timedelta(days=1)
        return day.isoformat(), (day + timedelta(days=1)).isoformat()
    if "hoje" in q:
        return today.isoformat(), (today + timedelta(days=1)).isoformat()
    if "semana" in q:
        start = today - timedelta(days=7)
        return start.isoformat(), (today + timedelta(days=1)).isoformat()
    if any(w in q for w in ("mes", "mês")):
        start = today - timedelta(days=30)
        return start.isoformat(), (today + timedelta(days=1)).isoformat()
    return None


def search_segments(
    conn: sqlite3.Connection, question: str, *, limit: int = MAX_SEGMENTS
) -> list[Source]:
    """Trechos relevantes para a pergunta, do mais recente para o mais antigo."""
    terms = _extract_terms(question)
    period = _parse_period(question)

    rows: list[sqlite3.Row] = []

    if terms:
        # OR em vez de AND: quem pergunta raramente usa as palavras exatas da
        # transcrição, e um resultado a mais é melhor que nenhum.
        match = " OR ".join(terms)
        sql = """
            SELECT s.id, s.started_at, s.transcript, ss.source,
                   COALESCE(s.app_name, ss.app_name) AS app_name
              FROM segments_fts f
              JOIN segments s  ON s.id = f.rowid
              JOIN sessions ss ON ss.id = s.session_id
             WHERE segments_fts MATCH ?
        """
        params: list = [match]
        if period:
            sql += " AND s.started_at >= ? AND s.started_at < ?"
            params += [period[0], period[1]]
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            # Pergunta com sintaxe que o FTS rejeita — cai para o período.
            rows = []

    # Sem termos úteis ("o que aconteceu ontem?"), o período é a busca.
    if not rows and period:
        rows = conn.execute(
            """
            SELECT s.id, s.started_at, s.transcript, ss.source,
                   COALESCE(s.app_name, ss.app_name) AS app_name
              FROM segments s JOIN sessions ss ON ss.id = s.session_id
             WHERE s.started_at >= ? AND s.started_at < ?
               AND s.transcript IS NOT NULL AND TRIM(s.transcript) != ''
             ORDER BY s.started_at DESC LIMIT ?
            """,
            (period[0], period[1], limit),
        ).fetchall()

    sources: list[Source] = []
    for row in rows:
        # Série e música não respondem pergunta sobre o que *você* fez.
        category = classify_app(row["app_name"], row["source"])
        if not is_report_worthy(category):
            continue
        text = (row["transcript"] or "").strip()
        if not text:
            continue
        sources.append(
            Source(
                segment_id=row["id"],
                started_at=row["started_at"],
                source=row["source"],
                text=text[:SNIPPET_CHARS],
            )
        )

    return sources


def build_prompt(question: str, sources: list[Source]) -> str:
    """Monta o contexto para o LLM."""
    lines = []
    for i, s in enumerate(sources, 1):
        when = datetime.fromisoformat(s.started_at)
        label = {"mic": "você", "system": "áudio do sistema"}.get(s.source, s.source)
        lines.append(f"[{i}] {when:%d/%m %H:%M} ({label}): {s.text}")

    return (
        f"Pergunta: {question}\n\n"
        f"Trechos da transcrição que podem responder:\n\n" + "\n\n".join(lines)
    )


SYSTEM_PROMPT = """\
Você responde perguntas sobre o que a pessoa falou ou ouviu, com base na
transcrição do áudio dela. Responda em português do Brasil, direto ao ponto.

A transcrição é automática e imperfeita: espere nomes próprios errados e
frases cortadas.

Regras:

Responda apenas com o que os trechos sustentam. Se eles não respondem a
pergunta, diga isso — não complete com suposição.

Cite os trechos que embasam cada afirmação usando o número entre colchetes,
assim: [1], [3]. É como a pessoa confere se você não inventou.

Se os trechos forem ambíguos, diga o que está claro e o que não está.

Não moralize nem comente os hábitos da pessoa. Responda o que foi perguntado.
"""


async def answer(chain: ProviderChain, question: str) -> dict:
    """Responde uma pergunta sobre o histórico."""
    sources = search_segments(db.get_connection(), question)

    if not sources:
        return {
            "answer": (
                "Não encontrei nada na transcrição sobre isso. Pode ser que não "
                "tenha sido capturado, ou que a pergunta use palavras diferentes "
                "das que aparecem na gravação."
            ),
            "sources": [],
            "provider": None,
            "cost_cents": 0.0,
        }

    # O provedor `echo` só reorganiza texto — como último recurso de relatório
    # faz sentido, mas responder uma pergunta com um índice de trechos não. É
    # melhor dizer que falta configurar do que devolver algo sem sentido.
    real_providers = [p for p in chain.providers if p.name != "echo"]
    if not real_providers:
        return {
            "answer": (
                "O chat precisa de um provedor de linguagem configurado. "
                "Abra o Hub para ver o estado, ou configure `llm.providers` no "
                "config.local.yaml — dá para usar o Ollama local, sem custo."
            ),
            "sources": [
                {
                    "n": i,
                    "segment_id": s.segment_id,
                    "started_at": s.started_at,
                    "source": s.source,
                    "text": s.text,
                }
                for i, s in enumerate(sources[:10], 1)
            ],
            "provider": None,
            "cost_cents": 0.0,
        }

    prompt = build_prompt(question, sources)
    estimated_in = len(prompt) // 4

    result = await chain.run(
        lambda p: p.complete(prompt, system=SYSTEM_PROMPT, max_tokens=2000),
        cost_estimator=lambda p: p.estimate_cost_cents(estimated_in, 800),
    )

    return {
        "answer": result.value.text,
        "sources": [
            {
                "n": i,
                "segment_id": s.segment_id,
                "started_at": s.started_at,
                "source": s.source,
                "text": s.text,
            }
            for i, s in enumerate(sources, 1)
        ],
        "provider": result.provider,
        "cost_cents": result.value.cost_cents,
    }
