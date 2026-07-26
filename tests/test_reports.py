"""Testes da geração de relatórios.

Nenhuma chamada de API — o LLM é sempre um provedor falso.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta

import pytest

from server import db
from server.hub.chain import ProviderChain
from server.models import Completion
from server.reports.builder import build_day_context, build_day_prompt, fetch_day_segments
from server.reports.generator import NoMaterial, generate_daily, generate_monthly


class FakeLLM:
    """Devolve texto fixo e registra o prompt recebido, para inspeção."""

    name = "fake"
    requires_network = False

    def __init__(self, text: str = "# Relatório\n\nDia tranquilo."):
        self.text = text
        self.last_prompt: str | None = None
        self.last_system: str | None = None

    async def complete(self, prompt: str, *, system=None, max_tokens=None) -> Completion:
        self.last_prompt = prompt
        self.last_system = system
        return Completion(
            text=self.text, provider=self.name, tokens_in=1000, tokens_out=200,
            cost_cents=1.5,
        )

    async def health(self) -> bool:
        return True

    def estimate_cost_cents(self, tokens_in: int, tokens_out: int) -> float:
        return 1.5


def _seed_day(day: date, texts: list[str], *, source: str = "mic",
              base_hour: int = 9, duration_ms: int = 5000,
              spacing_seconds: int = 10, app_name: str | None = None) -> None:
    """Insere segmentos transcritos num dia.

    `spacing_seconds` fica abaixo do BLOCK_GAP por padrão, então os segmentos
    contam como contíguos; passe um valor maior para forçar blocos separados.
    """
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO sessions(device_id, source, started_at) VALUES ('pc', ?, ?)",
            (source, datetime.combine(day, datetime.min.time()).isoformat()),
        )
        session_id = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
        base = datetime(day.year, day.month, day.day, base_hour, 0, 0)
        for i, text in enumerate(texts):
            started = base + timedelta(seconds=i * spacing_seconds)
            conn.execute(
                """
                INSERT INTO segments
                    (session_id, client_uid, started_at, duration_ms, transcript,
                     app_name, status)
                VALUES (?, ?, ?, ?, ?, ?, 'done')
                """,
                # a hora entra no uid porque um teste chama _seed_day duas vezes
                # no mesmo dia e fonte (manhã e tarde)
                (session_id, f"uid-{day}-{source}-{base_hour:02d}-{i:04d}",
                 started.isoformat(), duration_ms, text, app_name),
            )


# ─────────────────────────────── builder ───────────────────────────────


def test_agrupa_segmentos_contiguos_da_mesma_fonte(temp_db):
    """400 linhas de uma frase cada viram parágrafos legíveis."""
    day = date(2026, 7, 20)
    _seed_day(day, ["primeira frase", "segunda frase", "terceira frase"])

    context = build_day_context(fetch_day_segments(db.get_connection(), day))

    assert context.segment_count == 3
    assert context.text.count("[") == 1, "trechos contíguos devem virar um bloco só"
    assert "primeira frase segunda frase terceira frase" in context.text


def test_fontes_diferentes_ficam_em_blocos_separados(temp_db):
    day = date(2026, 7, 20)
    _seed_day(day, ["eu falando"], source="mic", base_hour=9)
    _seed_day(day, ["video tocando"], source="system", base_hour=10)

    context = build_day_context(fetch_day_segments(db.get_connection(), day))

    assert "(microfone)" in context.text
    assert "(sistema)" in context.text


def test_lacuna_longa_separa_blocos(temp_db):
    """Manhã e tarde não devem virar um parágrafo só."""
    day = date(2026, 7, 20)
    _seed_day(day, ["reunião da manhã"], base_hour=9)
    _seed_day(day, ["trabalho da tarde"], base_hour=15)

    context = build_day_context(fetch_day_segments(db.get_connection(), day))

    assert context.text.count("[") == 2


def test_dia_muito_grande_e_truncado_mantendo_os_trechos_longos(temp_db):
    """O corte tira interjeições, não as falas com conteúdo."""
    day = date(2026, 7, 20)
    longos = ["decisão importante sobre o orçamento do projeto " * 20 for _ in range(5)]
    curtos = ["sim", "tá", "uhum", "certo", "ok"] * 20
    _seed_day(day, longos + curtos)

    context = build_day_context(
        fetch_day_segments(db.get_connection(), day), max_tokens=500
    )

    assert context.truncated is True
    assert context.included_count < context.segment_count
    assert "decisão importante" in context.text, "trechos longos devem sobreviver ao corte"


def test_prompt_avisa_quando_o_dia_foi_truncado(temp_db):
    day = date(2026, 7, 20)
    _seed_day(day, ["frase com bastante conteúdo aqui " * 30 for _ in range(10)])

    context = build_day_context(
        fetch_day_segments(db.get_connection(), day), max_tokens=200
    )
    prompt = build_day_prompt(day, context)

    assert "AVISO" in prompt, "o modelo precisa saber que o material está incompleto"


def test_prompt_traz_data_e_dia_da_semana(temp_db):
    day = date(2026, 7, 20)  # segunda-feira
    _seed_day(day, ["olá"])

    prompt = build_day_prompt(day, build_day_context(fetch_day_segments(db.get_connection(), day)))

    assert "20/07/2026" in prompt
    assert "segunda-feira" in prompt


def test_segmentos_vazios_sao_ignorados(temp_db):
    """Transcrição vazia é comum (ruído) e não deve poluir o contexto."""
    day = date(2026, 7, 20)
    _seed_day(day, ["conteúdo real", "   ", ""])

    rows = fetch_day_segments(db.get_connection(), day)

    assert len(rows) == 1


# ───────────────────── filtro por origem do áudio ─────────────────────


def test_serie_no_netflix_fica_fora_do_relatorio(temp_db):
    """O caso que motivou o filtro: um dia com série e reunião.

    O relatório deve falar da reunião, não do enredo da série.
    """
    day = date(2026, 7, 20)
    _seed_day(day, ["decidimos adiar a entrega para sexta"],
              source="system", base_hour=10, app_name="teams.exe")
    _seed_day(day, ["você nunca vai adivinhar quem é o assassino"],
              source="system", base_hour=21, app_name="netflix.exe")

    context = build_day_context(fetch_day_segments(db.get_connection(), day))

    assert "adiar a entrega" in context.text
    assert "assassino" not in context.text, "série não pode entrar no relatório"
    assert context.skipped_entertainment == 1


def test_musica_de_fundo_nao_derruba_a_reuniao(temp_db):
    """Spotify tocando durante o Teams não pode custar a reunião."""
    day = date(2026, 7, 20)
    _seed_day(day, ["revisão do orçamento com o time"],
              source="system", app_name="teams.exe+spotify.exe")

    context = build_day_context(fetch_day_segments(db.get_connection(), day))

    assert "orçamento" in context.text
    assert context.skipped_entertainment == 0


def test_microfone_entra_mesmo_com_netflix_tocando(temp_db):
    """Você falando é sempre relevante — o filtro é só do áudio do sistema."""
    day = date(2026, 7, 20)
    _seed_day(day, ["preciso lembrar de ligar para o cliente"],
              source="mic", base_hour=21, app_name="netflix.exe")

    context = build_day_context(fetch_day_segments(db.get_connection(), day))

    assert "ligar para o cliente" in context.text
    assert context.skipped_entertainment == 0


def test_prompt_avisa_sobre_o_tempo_descartado(temp_db):
    """O modelo precisa saber que houve tempo omitido, para não inventar."""
    day = date(2026, 7, 20)
    _seed_day(day, ["conversa de trabalho"], source="system", app_name="zoom.exe")
    _seed_day(day, ["episódio da série"] * 3, source="system",
              base_hour=20, app_name="netflix.exe")

    context = build_day_context(fetch_day_segments(db.get_connection(), day))
    prompt = build_day_prompt(day, context)

    assert "série, música ou jogo" in prompt
    assert "Não os mencione" in prompt


def test_dia_so_de_entretenimento_nao_gera_relatorio(temp_db):
    """Um sábado inteiro de Netflix não deve virar relatório de trabalho."""
    day = date(2026, 7, 25)
    _seed_day(day, ["cena da série"] * 5, source="system", app_name="netflix.exe")

    llm = FakeLLM()
    with pytest.raises(NoMaterial):
        asyncio.run(generate_daily(ProviderChain("llm", [llm]), day))

    assert llm.last_prompt is None, "não deve gastar token com um dia só de série"


def test_navegador_entra_porque_pode_ser_reuniao(temp_db):
    """msedge é ambíguo: Meet ou YouTube. Na dúvida, incluir."""
    day = date(2026, 7, 20)
    _seed_day(day, ["alinhamento sobre o roadmap"],
              source="system", app_name="msedge.exe")

    context = build_day_context(fetch_day_segments(db.get_connection(), day))

    assert "roadmap" in context.text
    assert context.skipped_entertainment == 0


# ────────────────────────────── generator ──────────────────────────────


def test_gera_e_persiste_o_relatorio_diario(temp_db):
    day = date(2026, 7, 20)
    _seed_day(day, ["reunião sobre o orçamento", "decidimos adiar a entrega"])

    llm = FakeLLM("# 20 de julho\n\nDia de reuniões.")
    chain = ProviderChain("llm", [llm])

    result = asyncio.run(generate_daily(chain, day))

    assert result["provider"] == "fake"
    row = db.get_connection().execute(
        "SELECT content_md, cost_cents, tokens_in FROM reports WHERE id = ?",
        (result["id"],),
    ).fetchone()
    assert row["content_md"] == "# 20 de julho\n\nDia de reuniões."
    assert row["cost_cents"] == 1.5
    assert row["tokens_in"] == 1000


def test_relatorio_recebe_a_transcricao_e_o_prompt_do_arquivo(temp_db):
    day = date(2026, 7, 20)
    _seed_day(day, ["falei sobre o orçamento"])

    llm = FakeLLM()
    asyncio.run(generate_daily(ProviderChain("llm", [llm]), day))

    assert "orçamento" in llm.last_prompt
    assert "português do Brasil" in llm.last_system, "deve usar o prompt de prompts/daily.md"


def test_dia_sem_transcricao_levanta_no_material(temp_db):
    """Não gasta token com um dia vazio."""
    llm = FakeLLM()
    chain = ProviderChain("llm", [llm])

    with pytest.raises(NoMaterial):
        asyncio.run(generate_daily(chain, date(2026, 7, 21)))

    assert llm.last_prompt is None, "não deve chamar o LLM sem material"


def test_regerar_o_mesmo_dia_substitui_em_vez_de_duplicar(temp_db):
    day = date(2026, 7, 20)
    _seed_day(day, ["conteúdo"])

    asyncio.run(generate_daily(ProviderChain("llm", [FakeLLM("primeira versão")]), day))
    asyncio.run(generate_daily(ProviderChain("llm", [FakeLLM("segunda versão")]), day))

    rows = db.get_connection().execute(
        "SELECT content_md FROM reports WHERE type = 'daily' AND period_start = ?",
        (day.isoformat(),),
    ).fetchall()

    assert len(rows) == 1, "regerar não pode duplicar"
    assert rows[0]["content_md"] == "segunda versão"


def test_id_devolvido_ao_regerar_aponta_para_o_relatorio_certo(temp_db):
    """No caminho de UPDATE, lastrowid do SQLite devolve lixo.

    A API respondia com um id inexistente, e abrir o relatório recém-gerado
    dava 404.
    """
    day = date(2026, 7, 20)
    _seed_day(day, ["conteúdo do dia"])

    primeiro = asyncio.run(generate_daily(ProviderChain("llm", [FakeLLM("v1")]), day))
    segundo = asyncio.run(generate_daily(ProviderChain("llm", [FakeLLM("v2")]), day))

    assert primeiro["id"] == segundo["id"], "regerar deve devolver o mesmo id"

    row = db.get_connection().execute(
        "SELECT content_md FROM reports WHERE id = ?", (segundo["id"],)
    ).fetchone()
    assert row is not None, "o id devolvido tem que existir na tabela"
    assert row["content_md"] == "v2"


def test_relatorio_mensal_consome_os_diarios(temp_db):
    for dia in (10, 11, 12):
        day = date(2026, 7, dia)
        _seed_day(day, [f"trabalho do dia {dia}"])
        asyncio.run(generate_daily(ProviderChain("llm", [FakeLLM(f"resumo {dia}")]), day))

    llm = FakeLLM("# Julho\n\nMês produtivo.")
    result = asyncio.run(generate_monthly(ProviderChain("llm", [llm]), 2026, 7))

    assert result["days_included"] == 3
    assert "resumo 10" in llm.last_prompt, "o mensal deve ler os diários"
    assert "trabalho do dia 10" not in llm.last_prompt, "não deve reler a transcrição bruta"


def test_mes_sem_diarios_levanta_no_material(temp_db):
    llm = FakeLLM()
    with pytest.raises(NoMaterial, match="gere os diários primeiro"):
        asyncio.run(generate_monthly(ProviderChain("llm", [llm]), 2026, 3))

    assert llm.last_prompt is None


def test_relatorio_cai_para_o_provedor_de_reserva(temp_db):
    """Claude fora do ar não impede o relatório do dia."""
    from server.hub.base import ProviderError

    class Broken(FakeLLM):
        name = "quebrado"

        async def complete(self, prompt, *, system=None, max_tokens=None):
            raise ProviderError("indisponível", provider=self.name)

    day = date(2026, 7, 20)
    _seed_day(day, ["conteúdo do dia"])

    backup = FakeLLM("relatório do reserva")
    backup.name = "reserva"
    result = asyncio.run(generate_daily(ProviderChain("llm", [Broken(), backup]), day))

    assert result["provider"] == "reserva"
