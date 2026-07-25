"""Cadeia de provedores com fallback, circuit breaker e teto de gasto.

Tenta cada provedor na ordem configurada. Um provedor que falha N vezes
seguidas tem o circuito aberto por um tempo e é pulado; o próximo assume.
Todo uso é registrado em provider_usage — é daí que saem o teto diário e o
dashboard de custos.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Generic, TypeVar

from .. import db
from ..models import ProviderHealth
from .base import BudgetExceeded, ProviderError

log = logging.getLogger(__name__)

P = TypeVar("P")
R = TypeVar("R")


@dataclass
class _Breaker:
    """Circuit breaker por provedor."""

    failure_threshold: int
    reset_after_seconds: float
    consecutive_failures: int = 0
    opened_at: float | None = None
    last_error: str | None = None

    def is_open(self) -> bool:
        if self.opened_at is None:
            return False
        if time.monotonic() - self.opened_at >= self.reset_after_seconds:
            # Meia-abertura: deixa passar uma tentativa. Se falhar, reabre.
            self.opened_at = None
            self.consecutive_failures = self.failure_threshold - 1
            return False
        return True

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.opened_at = None
        self.last_error = None

    def record_failure(self, error: str) -> None:
        self.consecutive_failures += 1
        self.last_error = error
        if self.consecutive_failures >= self.failure_threshold:
            self.opened_at = time.monotonic()


@dataclass
class ChainResult(Generic[R]):
    value: R
    provider: str
    cost_cents: float
    latency_ms: int
    attempts: list[str] = field(default_factory=list)


class ProviderChain(Generic[P]):
    """Executa uma operação contra provedores em ordem, até um funcionar."""

    def __init__(
        self,
        hub: str,
        providers: list[P],
        *,
        daily_budget_cents: float = 0.0,
        failure_threshold: int = 3,
        reset_after_seconds: float = 300.0,
    ):
        self.hub = hub
        self.providers = providers
        self.daily_budget_cents = daily_budget_cents
        self._breakers: dict[str, _Breaker] = {
            p.name: _Breaker(failure_threshold, reset_after_seconds) for p in providers
        }

    # ────────────────────────────── gasto ──────────────────────────────

    def spent_today_cents(self) -> float:
        row = db.get_connection().execute(
            """
            SELECT COALESCE(SUM(cost_cents), 0) AS total
              FROM provider_usage
             WHERE hub = ? AND date(created_at) = date('now', 'localtime')
            """,
            (self.hub,),
        ).fetchone()
        return float(row["total"])

    def _budget_allows(self, estimated_cents: float) -> bool:
        """Cabe no teto contando o custo previsto desta chamada?

        Compara `gasto + estimativa` contra o teto — só olhar o gasto atual
        deixaria uma chamada cara passar quando o saldo restante é menor que
        ela, estourando o limite.
        """
        if self.daily_budget_cents <= 0 or estimated_cents <= 0:
            return True
        return self.spent_today_cents() + estimated_cents <= self.daily_budget_cents

    def _record(
        self, provider: str, ok: bool, cost: float, latency_ms: int, error: str | None
    ) -> None:
        try:
            with db.transaction() as conn:
                conn.execute(
                    """
                    INSERT INTO provider_usage
                        (hub, provider, ok, cost_cents, latency_ms, error)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (self.hub, provider, int(ok), cost, latency_ms, error),
                )
        except Exception:
            # Telemetria nunca derruba a operação principal.
            log.exception("falha ao registrar uso do provedor %s", provider)

    # ───────────────────────────── execução ─────────────────────────────

    async def run(
        self,
        operation: Callable[[P], Awaitable[R]],
        *,
        cost_estimator: Callable[[P], float] | None = None,
    ) -> ChainResult[R]:
        """Roda `operation` no primeiro provedor que conseguir atender.

        Provedores são pulados quando: estão com o circuito aberto, ou custam
        dinheiro e o teto diário já foi atingido.
        """
        attempts: list[str] = []
        errors: list[str] = []
        skipped_for_budget = False

        for provider in self.providers:
            name = provider.name
            breaker = self._breakers[name]

            if breaker.is_open():
                log.debug("[%s] %s: circuito aberto, pulando", self.hub, name)
                errors.append(f"{name}: circuito aberto")
                continue

            estimated = cost_estimator(provider) if cost_estimator else 0.0
            if not self._budget_allows(estimated):
                log.warning(
                    "[%s] %s: teto diário não comporta a chamada "
                    "(gasto %.2f + previsto %.2f > teto %.2f centavos), pulando",
                    self.hub, name, self.spent_today_cents(), estimated,
                    self.daily_budget_cents,
                )
                errors.append(f"{name}: teto de gasto")
                skipped_for_budget = True
                continue

            attempts.append(name)
            started = time.monotonic()
            try:
                value = await operation(provider)
            except ProviderError as exc:
                latency = int((time.monotonic() - started) * 1000)
                breaker.record_failure(str(exc))
                self._record(name, False, 0.0, latency, str(exc))
                errors.append(f"{name}: {exc}")
                log.warning("[%s] %s falhou: %s", self.hub, name, exc)
                if not exc.retryable:
                    # A requisição em si é inválida — não adianta tentar outro.
                    raise
                continue
            except Exception as exc:
                latency = int((time.monotonic() - started) * 1000)
                breaker.record_failure(repr(exc))
                self._record(name, False, 0.0, latency, repr(exc))
                errors.append(f"{name}: {exc!r}")
                log.exception("[%s] %s erro inesperado", self.hub, name)
                continue

            latency = int((time.monotonic() - started) * 1000)
            cost = float(getattr(value, "cost_cents", 0.0) or 0.0)
            breaker.record_success()
            self._record(name, True, cost, latency, None)
            return ChainResult(value, name, cost, latency, attempts)

        detail = "; ".join(errors) or "nenhum provedor configurado"
        if skipped_for_budget and not attempts:
            raise BudgetExceeded(f"[{self.hub}] teto diário atingido — {detail}")
        raise ProviderError(f"[{self.hub}] todos os provedores falharam — {detail}")

    # ───────────────────────────── inspeção ─────────────────────────────

    async def health(self) -> list[ProviderHealth]:
        results: list[ProviderHealth] = []
        for provider in self.providers:
            breaker = self._breakers[provider.name]
            try:
                available = await provider.health()
                detail = breaker.last_error
            except Exception as exc:
                available = False
                detail = repr(exc)
            results.append(
                ProviderHealth(
                    name=provider.name,
                    available=available,
                    circuit_open=breaker.is_open(),
                    consecutive_failures=breaker.consecutive_failures,
                    detail=detail,
                )
            )
        return results

    @property
    def chain_names(self) -> list[str]:
        return [p.name for p in self.providers]
