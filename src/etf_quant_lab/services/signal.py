"""Application service for idempotent, quality-gated daily signal generation."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal

from etf_quant_lab.contracts.enums import (
    Exchange,
    QualityGateStatus,
    RiskState,
    SignalAction,
)
from etf_quant_lab.contracts.errors import DomainError
from etf_quant_lab.contracts.signal import (
    DailySignalBatch,
    GenerateDailySignalRequest,
    SignalItem,
    SignalStatus,
    signal_idempotency_key,
    universe_hash,
)
from etf_quant_lab.contracts.strategy import TargetPortfolio, ValidateParametersRequest
from etf_quant_lab.domain.market_view import MarketDataView
from etf_quant_lab.domain.repositories import TradingCalendarRepository
from etf_quant_lab.ids import IdGenerator
from etf_quant_lab.services.quality import QualityService
from etf_quant_lab.services.strategy import StrategyService
from etf_quant_lab.storage.repositories import DataBatchRepository
from etf_quant_lab.storage.signal import SignalRepository

SIGNAL_NOT_TRADING_DAY = "SIG_NOT_TRADING_DAY"
SIGNAL_DATA_STALE = "SIG_DATA_STALE"
SIGNAL_QUALITY_BLOCKED = "SIG_QUALITY_BLOCKED"

_BLOCK_REASON_STALE = "行情最新交易日早于请求交易日"
_BLOCK_REASON_QUALITY = "数据质量门禁未通过"


class SignalService:
    """Generate one auditable daily signal batch per unique research context.

    The same ``(strategy_version, trade_date, universe_hash, parameter_hash)``
    always resolves to the same stored batch; blocked contexts persist a BLOCKED
    batch instead of a tradable one so the day's decision trail is never empty.
    """

    def __init__(
        self,
        *,
        strategy_service: StrategyService,
        quality_service: QualityService,
        batch_repository: DataBatchRepository,
        calendar_repository: TradingCalendarRepository,
        signal_repository: SignalRepository,
        id_generator: IdGenerator,
        universe_symbols: tuple[str, ...],
        exchange: Exchange = Exchange.SSE,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._strategies = strategy_service
        self._quality = quality_service
        self._batches = batch_repository
        self._calendar = calendar_repository
        self._signals = signal_repository
        self._id_generator = id_generator
        self._universe_symbols = tuple(sorted(universe_symbols))
        self._exchange = exchange
        self._clock = clock or (lambda: datetime.now(UTC))

    def generate_daily(self, request: GenerateDailySignalRequest) -> DailySignalBatch:
        """Generate or idempotently return the signal batch for one trade date."""

        self._require_trading_day(request)
        validation = self._strategies.validate_parameters(
            ValidateParametersRequest(
                strategy_id=request.strategy_id,
                version=request.strategy_version,
                parameters=request.parameters,
            )
        )
        if not validation.valid or validation.parameter_hash is None:
            raise DomainError(
                "STRAT_PARAMS_INVALID",
                "策略参数校验失败",
                details={"errors": validation.errors},
            )

        universe_digest = universe_hash(self._universe_symbols)
        idempotency_key = signal_idempotency_key(
            strategy_id=request.strategy_id,
            strategy_version=request.strategy_version,
            trade_date=request.trade_date,
            universe_digest=universe_digest,
            parameter_digest=validation.parameter_hash,
        )
        existing = self._signals.find_by_idempotency_key(idempotency_key)
        if existing is not None:
            return existing

        bars = self._batches.query_daily_bars(
            symbols=self._universe_symbols,
            end_date=request.trade_date,
        )
        data_as_of = max((bar.trade_date for bar in bars), default=None)

        if data_as_of is None or (
            data_as_of < request.trade_date and not request.allow_stale_data
        ):
            return self._persist_blocked(
                request,
                validation.parameter_hash,
                universe_digest,
                idempotency_key,
                reason=_BLOCK_REASON_STALE,
                data_as_of=data_as_of or request.trade_date,
            )

        if request.quality_report_id is not None:
            report = None
            if request.dataset_id is not None:
                report = self._quality.get_report(request.dataset_id)
            if report is not None and report.gate_status == QualityGateStatus.FAILED:
                return self._persist_blocked(
                    request,
                    validation.parameter_hash,
                    universe_digest,
                    idempotency_key,
                    reason=_BLOCK_REASON_QUALITY,
                    data_as_of=data_as_of,
                )

        market_data = MarketDataView(as_of_date=request.trade_date, bars=bars)
        portfolio = self._strategies.generate_targets(
            strategy_id=request.strategy_id,
            version=request.strategy_version,
            parameters=request.parameters,
            as_of_date=request.trade_date,
            universe_symbols=self._universe_symbols,
            market_data=market_data,
            current_weights=request.current_weights,
            cash_weight=self._cash_weight(request),
        )

        items = self._build_items(request, market_data, portfolio)
        batch = DailySignalBatch(
            signal_id=self._id_generator.new(),
            trade_date=request.trade_date,
            generated_at=self._clock(),
            strategy_id=request.strategy_id,
            strategy_version=request.strategy_version,
            parameter_hash=validation.parameter_hash,
            universe_hash=universe_digest,
            idempotency_key=idempotency_key,
            status=SignalStatus.VALID,
            risk_state=RiskState.NORMAL,
            items=items,
            target_cash_weight=portfolio.cash_weight,
            data_as_of=data_as_of,
            dataset_id=request.dataset_id,
            quality_report_id=request.quality_report_id,
            warnings=portfolio.warnings,
        )
        self._signals.save(batch)
        return batch

    def get(self, signal_id: str) -> DailySignalBatch:
        batch = self._signals.get(signal_id)
        if batch is None:
            raise DomainError(
                "SIG_NOT_FOUND",
                "信号不存在",
                details={"signal_id": signal_id},
            )
        return batch

    def _require_trading_day(self, request: GenerateDailySignalRequest) -> None:
        day = self._calendar.get_day(self._exchange, request.trade_date)
        if day is None or not day.is_open:
            raise DomainError(
                SIGNAL_NOT_TRADING_DAY,
                "请求日期不是有效交易日",
                details={"trade_date": request.trade_date.isoformat()},
            )

    def _persist_blocked(
        self,
        request: GenerateDailySignalRequest,
        parameter_hash: str,
        universe_digest: str,
        idempotency_key: str,
        *,
        reason: str,
        data_as_of: date,
    ) -> DailySignalBatch:
        batch = DailySignalBatch(
            signal_id=self._id_generator.new(),
            trade_date=request.trade_date,
            generated_at=self._clock(),
            strategy_id=request.strategy_id,
            strategy_version=request.strategy_version,
            parameter_hash=parameter_hash,
            universe_hash=universe_digest,
            idempotency_key=idempotency_key,
            status=SignalStatus.BLOCKED,
            risk_state=RiskState.BLOCKED,
            items=(),
            target_cash_weight=Decimal(1),
            data_as_of=data_as_of,
            dataset_id=request.dataset_id,
            quality_report_id=request.quality_report_id,
            blocked_reason=reason,
        )
        self._signals.save(batch)
        return batch

    def _cash_weight(self, request: GenerateDailySignalRequest) -> Decimal:
        invested = sum(request.current_weights.values(), Decimal(0))
        return max(Decimal(0), Decimal(1) - invested)

    def _build_items(
        self,
        request: GenerateDailySignalRequest,
        market_data: MarketDataView,
        portfolio: TargetPortfolio,
    ) -> tuple[SignalItem, ...]:
        current = dict(request.current_weights)
        items: list[SignalItem] = []
        targeted = {allocation.symbol for allocation in portfolio.allocations}

        for allocation in portfolio.allocations:
            current_weight = current.get(allocation.symbol, Decimal(0))
            delta = allocation.target_weight - current_weight
            latest = market_data.latest(allocation.symbol)
            items.append(
                SignalItem(
                    symbol=allocation.symbol,
                    action=allocation.action,
                    current_weight=current_weight,
                    target_weight=allocation.target_weight,
                    weight_delta=delta,
                    reference_close=None if latest is None else latest.close,
                    score=allocation.score,
                    reason_codes=allocation.reason_codes,
                )
            )
        # Symbols currently held but absent from the target must be exited.
        for symbol, weight in sorted(current.items()):
            if symbol in targeted or weight <= 0:
                continue
            latest = market_data.latest(symbol)
            items.append(
                SignalItem(
                    symbol=symbol,
                    action=SignalAction.SELL,
                    current_weight=weight,
                    target_weight=Decimal(0),
                    weight_delta=-weight,
                    reference_close=None if latest is None else latest.close,
                    reason_codes=("EXIT_NOT_IN_TARGET",),
                )
            )
        return tuple(items)
