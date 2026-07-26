"""Testes do hub de LLM.

Provedores reais não são chamados — nenhuma chave, nenhuma requisição paga.
"""

from __future__ import annotations

import asyncio

import pytest

from server.config import Config
from server.hub.base import ProviderError
from server.hub.llm import build_llm_chain
from server.models import Completion


class FakeLLM:
    requires_network = False

    def __init__(self, name: str, *, fail: bool = False, cost: float = 0.0, text: str = "ok"):
        self.name = name
        self.fail = fail
        self.cost = cost
        self.text = text
        self.calls = 0

    async def complete(self, prompt: str, *, system=None, max_tokens=None) -> Completion:
        self.calls += 1
        if self.fail:
            raise ProviderError(f"{self.name} indisponível", provider=self.name)
        return Completion(
            text=self.text, provider=self.name, tokens_in=100, tokens_out=50,
            cost_cents=self.cost,
        )

    async def health(self) -> bool:
        return not self.fail

    def estimate_cost_cents(self, tokens_in: int, tokens_out: int) -> float:
        return self.cost


def test_cai_para_o_provedor_local_quando_a_api_falha(temp_db):
    """Cenário real: Claude fora do ar, Ollama assume."""
    from server.hub.chain import ProviderChain

    claude = FakeLLM("claude", fail=True, cost=5.0)
    ollama = FakeLLM("ollama", text="resumo local")
    chain = ProviderChain("llm", [claude, ollama])

    result = asyncio.run(chain.run(lambda p: p.complete("resuma isto")))

    assert result.provider == "ollama"
    assert result.value.text == "resumo local"


def test_teto_de_gasto_do_llm_preserva_o_provedor_local(temp_db):
    """Estourado o teto, o pago é pulado e o local continua atendendo."""
    from server.hub.chain import ProviderChain

    claude = FakeLLM("claude", cost=400.0)
    ollama = FakeLLM("ollama", cost=0.0)
    chain = ProviderChain("llm", [claude, ollama], daily_budget_cents=500.0)

    async def scenario():
        first = await chain.run(
            lambda p: p.complete("a"), cost_estimator=lambda p: p.estimate_cost_cents(0, 0)
        )
        second = await chain.run(
            lambda p: p.complete("b"), cost_estimator=lambda p: p.estimate_cost_cents(0, 0)
        )
        return first, second

    first, second = asyncio.run(scenario())

    assert first.provider == "claude"
    assert second.provider == "ollama", "após gastar 400 de 500, o pago deve ser pulado"


def test_custo_do_llm_e_registrado(temp_db):
    from server import db
    from server.hub.chain import ProviderChain

    chain = ProviderChain("llm", [FakeLLM("claude", cost=2.5)])
    asyncio.run(chain.run(lambda p: p.complete("oi")))

    row = db.get_connection().execute(
        "SELECT hub, provider, cost_cents FROM provider_usage ORDER BY id DESC LIMIT 1"
    ).fetchone()

    assert row["hub"] == "llm"
    assert row["cost_cents"] == 2.5


def test_gasto_de_stt_e_llm_sao_contados_separadamente(temp_db):
    """Os dois hubs têm tetos independentes — um não pode consumir o do outro."""
    from server.hub.chain import ProviderChain

    llm = ProviderChain("llm", [FakeLLM("claude", cost=10.0)])
    stt = ProviderChain("stt", [FakeLLM("whisper", cost=3.0)])

    asyncio.run(llm.run(lambda p: p.complete("oi")))

    assert llm.spent_today_cents() == 10.0
    assert stt.spent_today_cents() == 0.0, "o gasto de LLM não pode contar como STT"


def test_cadeia_vazia_quando_nenhum_provedor_esta_ativo(temp_db):
    """Config padrão tem tudo desativado — a cadeia sobe vazia, sem estourar."""
    chain = build_llm_chain(Config({
        "llm": {
            "chain": ["claude", "ollama"],
            "providers": {
                "claude": {"type": "anthropic", "enabled": False},
                "ollama": {"type": "ollama", "enabled": False},
            },
        }
    }))

    assert chain.chain_names == []

    with pytest.raises(ProviderError):
        asyncio.run(chain.run(lambda p: p.complete("oi")))


def test_registry_monta_a_cadeia_a_partir_do_config(temp_db):
    chain = build_llm_chain(Config({
        "llm": {
            "chain": ["local", "desconhecido"],
            "daily_budget_cents": 100,
            "providers": {
                "local": {"type": "ollama", "base_url": "http://localhost:11434"},
                "desconhecido": {"type": "inexistente"},
            },
        }
    }))

    assert chain.chain_names == ["local"], "tipo desconhecido deve ser pulado"
    assert chain.daily_budget_cents == 100


def test_provedor_anthropic_sem_chave_falha_sem_tentar_rede(temp_db):
    from server.hub.llm import AnthropicProvider

    provider = AnthropicProvider("claude", {"api_key": None})

    with pytest.raises(ProviderError, match="api_key"):
        asyncio.run(provider.complete("oi"))

    assert asyncio.run(provider.health()) is False


def test_calculo_de_custo_do_anthropic(temp_db):
    from server.hub.llm import AnthropicProvider

    provider = AnthropicProvider("claude", {"model": "claude-sonnet-5"})

    # 1M de entrada = 300 centavos; 1M de saída = 1500 centavos
    assert provider.estimate_cost_cents(1_000_000, 0) == 300.0
    assert provider.estimate_cost_cents(0, 1_000_000) == 1500.0

    # Um relatório diário realista: ~50k entrada, ~2k saída
    cost = provider.estimate_cost_cents(50_000, 2_000)
    assert 0 < cost < 50, f"relatório diário deveria custar centavos, veio {cost}"
