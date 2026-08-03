"""Testes do endpoint de ditado.

Ao contrário de `/ingest`, que responde `pending` e transcreve depois, aqui
quem chamou está com a tecla na mão esperando o texto para digitar.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server import db, main
from server.hub.base import BudgetExceeded, ProviderError
from server.hub.chain import ChainResult
from server.models import Transcript

# Cabeçalho mínimo que passa por `validate_audio`.
AUDIO_VALIDO = b"OggS" + b"\x00" * 24 + b"OpusHead" + b"\x00" * 100


class CadeiaFalsa:
    """No lugar da cadeia real: carregar o large-v3 custa ~7s por teste."""

    def __init__(self, texto: str = "olá mundo", erro: Exception | None = None):
        self.texto = texto
        self.erro = erro
        self.chamadas = 0
        self.providers: list = []

    async def run(self, operacao, cost_estimator=None):
        self.chamadas += 1
        if self.erro is not None:
            raise self.erro
        return ChainResult(
            value=Transcript(
                text=self.texto, language="pt", confidence=0.9,
                provider="falso", cost_cents=0.0,
            ),
            provider="falso",
            cost_cents=0.0,
            latency_ms=42,
        )


@pytest.fixture
def api(temp_db, monkeypatch):
    monkeypatch.setattr(main, "stt_chain", CadeiaFalsa())
    return TestClient(main.app)


def _ditar(api, audio: bytes = AUDIO_VALIDO):
    return api.post("/api/dictate", files={"audio": ("d.opus", audio, "audio/ogg")})


def test_devolve_o_texto_na_mesma_resposta(api):
    """O ditado não pode responder 'pending' — o texto é para digitar agora."""
    r = _ditar(api)

    assert r.status_code == 200
    corpo = r.json()
    assert corpo["text"] == "olá mundo"
    assert corpo["provider"] == "falso"
    assert "status" not in corpo


def test_nao_cria_segmento_na_timeline(api):
    """Um ditado é comando, não registro de vida."""
    antes = db.get_connection().execute("SELECT COUNT(*) n FROM segments").fetchone()["n"]

    _ditar(api)

    depois = db.get_connection().execute("SELECT COUNT(*) n FROM segments").fetchone()["n"]
    assert depois == antes


def test_rejeita_audio_que_nao_e_opus(api):
    r = _ditar(api, b"RIFF" + b"\x00" * 200)

    assert r.status_code == 422
    assert "Ogg" in r.json()["detail"]


def test_rejeita_audio_vazio(api):
    assert _ditar(api, b"").status_code == 422


def test_teto_de_gasto_devolve_429(api, monkeypatch):
    """Sem isto o cliente trataria estouro de orçamento como falha do servidor."""
    monkeypatch.setattr(main, "stt_chain", CadeiaFalsa(erro=BudgetExceeded("teto")))

    assert _ditar(api).status_code == 429


def test_falha_do_provedor_devolve_502(api, monkeypatch):
    monkeypatch.setattr(main, "stt_chain", CadeiaFalsa(erro=ProviderError("caiu")))

    assert _ditar(api).status_code == 502


def test_o_temporario_some_mesmo_quando_o_provedor_falha(api, monkeypatch, tmp_path):
    """Ditar todo dia não pode encher o disco de arquivos órfãos."""
    monkeypatch.setattr(main.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(main, "stt_chain", CadeiaFalsa(erro=ProviderError("caiu")))

    _ditar(api)

    assert list(Path(tmp_path).glob("lifelog-ditado-*")) == []


def test_o_temporario_some_no_caminho_feliz(api, monkeypatch, tmp_path):
    monkeypatch.setattr(main.tempfile, "gettempdir", lambda: str(tmp_path))

    _ditar(api)

    assert list(Path(tmp_path).glob("lifelog-ditado-*")) == []


def test_texto_vem_sem_espacos_nas_bordas(api, monkeypatch):
    """O Whisper devolve com espaço inicial; digitar isso desalinharia o campo."""
    monkeypatch.setattr(main, "stt_chain", CadeiaFalsa(texto="  bom dia  "))

    assert _ditar(api).json()["text"] == "bom dia"


def test_sem_hub_devolve_503(api, monkeypatch):
    monkeypatch.setattr(main, "stt_chain", None)

    assert _ditar(api).status_code == 503


# ─────────────────────────────── aquecimento ───────────────────────────────


def test_aquecer_carrega_o_modelo(api, monkeypatch):
    """Chamado no press da tecla, esconde os ~7s de recarga do modelo."""
    carregou = []

    class ProviderComModelo:
        name = "whisper_falso"

        async def _get_model(self):
            carregou.append(True)

    cadeia = CadeiaFalsa()
    cadeia.providers = [ProviderComModelo()]
    monkeypatch.setattr(main, "stt_chain", cadeia)

    r = api.post("/api/dictate/aquecer")

    assert r.json()["aquecido"] == "whisper_falso"
    assert carregou == [True]


def test_aquecer_nao_estoura_quando_o_provedor_nao_tem_modelo(api, monkeypatch):
    """Provedor de nuvem não tem o que aquecer — e isso não é erro."""
    class ProviderDeNuvem:
        name = "deepgram_falso"

    cadeia = CadeiaFalsa()
    cadeia.providers = [ProviderDeNuvem()]
    monkeypatch.setattr(main, "stt_chain", cadeia)

    r = api.post("/api/dictate/aquecer")

    assert r.status_code == 200
    assert r.json()["aquecido"] is None
