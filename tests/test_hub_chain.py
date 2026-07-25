"""Testes do comportamento de fallback do hub.

Provedores falsos — nada de rede nem API paga.
"""

from __future__ import annotations

import asyncio

import pytest

from server.hub.base import BudgetExceeded, ProviderError
from server.hub.chain import ProviderChain
from server.models import Transcript


class FakeProvider:
    """Provedor controlável: falha as N primeiras chamadas, depois responde."""

    requires_network = False

    def __init__(self, name: str, *, fail_times: int = 0, cost: float = 0.0,
                 fatal: bool = False, healthy: bool = True):
        self.name = name
        self.fail_times = fail_times
        self.cost = cost
        self.fatal = fatal
        self.healthy = healthy
        self.calls = 0

    async def transcribe(self, audio=None, language="pt") -> Transcript:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ProviderError(
                f"{self.name} indisponível", provider=self.name, retryable=not self.fatal
            )
        return Transcript(text=f"ok-{self.name}", provider=self.name, cost_cents=self.cost)

    async def health(self) -> bool:
        return self.healthy

    def estimate_cost_cents(self, duration_ms: int) -> float:
        return self.cost


def _chain(providers, **kwargs) -> ProviderChain:
    return ProviderChain("stt", providers, **kwargs)


async def _run(chain: ProviderChain, **kwargs):
    return await chain.run(lambda p: p.transcribe(), **kwargs)


def test_usa_primeiro_provedor_saudavel(temp_db):
    primary = FakeProvider("primary")
    backup = FakeProvider("backup")
    result = asyncio.run(_run(_chain([primary, backup])))

    assert result.value.text == "ok-primary"
    assert result.provider == "primary"
    assert backup.calls == 0, "backup não deve ser chamado quando o primário funciona"


def test_cai_para_o_proximo_quando_o_primeiro_falha(temp_db):
    primary = FakeProvider("primary", fail_times=1)
    backup = FakeProvider("backup")
    result = asyncio.run(_run(_chain([primary, backup])))

    assert result.provider == "backup"
    assert result.attempts == ["primary", "backup"]


def test_erro_fatal_aborta_a_cadeia(temp_db):
    """Áudio corrompido não deve queimar os outros provedores."""
    primary = FakeProvider("primary", fail_times=1, fatal=True)
    backup = FakeProvider("backup")

    with pytest.raises(ProviderError):
        asyncio.run(_run(_chain([primary, backup])))

    assert backup.calls == 0


def test_todos_falhando_levanta_erro(temp_db):
    chain = _chain([FakeProvider("a", fail_times=9), FakeProvider("b", fail_times=9)])
    with pytest.raises(ProviderError, match="todos os provedores falharam"):
        asyncio.run(_run(chain))


def test_circuit_breaker_abre_e_pula_o_provedor(temp_db):
    primary = FakeProvider("primary", fail_times=99)
    backup = FakeProvider("backup")
    chain = _chain([primary, backup], failure_threshold=2, reset_after_seconds=300)

    async def scenario():
        for _ in range(3):
            await _run(chain)

    asyncio.run(scenario())

    # Após 2 falhas o circuito abre; a 3ª rodada nem tenta o primário.
    assert primary.calls == 2
    assert backup.calls == 3


def test_circuit_breaker_reabre_apos_o_tempo(temp_db):
    primary = FakeProvider("primary", fail_times=2)
    backup = FakeProvider("backup")
    chain = _chain([primary, backup], failure_threshold=2, reset_after_seconds=0)

    async def scenario():
        await _run(chain)   # primary falha (1)
        await _run(chain)   # primary falha (2) -> abre
        return await _run(chain)  # reset_after=0 -> meia-abertura, primary volta

    result = asyncio.run(scenario())
    assert result.provider == "primary", "circuito deve reabrir e o primário voltar"


def test_teto_de_gasto_pula_provedor_pago(temp_db):
    """Estourado o teto, provedores pagos são pulados e os locais continuam."""
    paid = FakeProvider("paid", cost=50.0)
    free = FakeProvider("free", cost=0.0)
    chain = _chain([paid, free], daily_budget_cents=60.0)

    async def scenario():
        first = await _run(chain, cost_estimator=lambda p: p.estimate_cost_cents(60000))
        second = await _run(chain, cost_estimator=lambda p: p.estimate_cost_cents(60000))
        return first, second

    first, second = asyncio.run(scenario())

    assert first.provider == "paid"
    assert second.provider == "free", "após gastar 50 de 60, o pago deve ser pulado"


def test_teto_estourado_sem_alternativa_levanta_budget_exceeded(temp_db):
    paid = FakeProvider("paid", cost=100.0)
    chain = _chain([paid], daily_budget_cents=50.0)

    async def scenario():
        await _run(chain, cost_estimator=lambda p: p.estimate_cost_cents(60000))
        await _run(chain, cost_estimator=lambda p: p.estimate_cost_cents(60000))

    with pytest.raises(BudgetExceeded):
        asyncio.run(scenario())


def test_uso_e_custo_sao_registrados(temp_db):
    from server import db

    chain = _chain([FakeProvider("primary", fail_times=1), FakeProvider("backup", cost=3.5)])
    asyncio.run(_run(chain))

    rows = db.get_connection().execute(
        "SELECT provider, ok, cost_cents FROM provider_usage ORDER BY id"
    ).fetchall()

    assert [(r["provider"], r["ok"]) for r in rows] == [("primary", 0), ("backup", 1)]
    assert rows[1]["cost_cents"] == 3.5
    assert chain.spent_today_cents() == 3.5


def test_health_reporta_estado_dos_provedores(temp_db):
    chain = _chain([FakeProvider("ok"), FakeProvider("down", healthy=False)])
    health = asyncio.run(chain.health())

    assert [h.name for h in health] == ["ok", "down"]
    assert health[0].available is True
    assert health[1].available is False
