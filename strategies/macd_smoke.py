from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import pandas as pd

from nautilus_trader.config import StrategyConfig
from nautilus_trader.common.enums import LogColor
from nautilus_trader.model.data import Bar
from nautilus_trader.model.data import BarType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.trading.strategy import Strategy


@dataclass(frozen=True)
class MacdSmokeSignalEvent:
    signal_date: date
    instrument_id: str
    signal_name: str
    signal_value: float
    score: float | None
    selected: bool
    reason: str
    extra: dict[str, Any]


@dataclass(frozen=True)
class MacdSmokeTargetEvent:
    target_id: str
    target_date: date
    execute_date: date
    instrument_id: str
    target_weight: float
    current_weight: float | None
    delta_weight: float | None
    reason: str
    extra: dict[str, Any]


@dataclass(frozen=True)
class MacdSmokeOrderEvent:
    order_id: str
    trading_date: date
    instrument_id: str
    side: str
    quantity: int
    target_weight: float
    status: str
    reason: str | None
    extra: dict[str, Any]


class MacdSmokeStrategyConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType
    trade_start_date: str
    trade_end_date: str
    short_period: int = 12
    long_period: int = 26
    signal_period: int = 9
    observation: int = 180
    entry_ma_period: int = 120
    exit_ma_period: int = 60
    target_percent: float = 0.6
    stop_loss: float = 0.08
    initial_cash: Decimal = Decimal("100000")
    timezone_name: str = "Asia/Shanghai"


class MacdSmokeStrategy(Strategy):
    """
    Reusable single-instrument MACD smoke strategy.

    Storage, data provider, venue symbol mapping, and result persistence are
    intentionally outside the strategy so the same trading logic can be reused
    by live trading wiring.
    """

    def __init__(self, config: MacdSmokeStrategyConfig) -> None:
        super().__init__(config)
        self._closes: list[float] = []
        self._raw_closes: list[float] = []
        self._volumes: list[float] = []
        self._dates: list[date] = []
        self._pending_target_weight: float | None = None
        self._pending_signal_date: date | None = None
        self._pending_reason: str | None = None
        self._entry_price: float | None = None
        self.signal_events: list[MacdSmokeSignalEvent] = []
        self.target_events: list[MacdSmokeTargetEvent] = []
        self.order_events: list[MacdSmokeOrderEvent] = []

    def on_start(self) -> None:
        self.subscribe_bars(self.config.bar_type)

    def on_bar(self, bar: Bar) -> None:
        trading_date = bar_date(bar, self.config.timezone_name)
        current_close = float(bar.close)

        self._submit_pending_target(trading_date, current_close)
        self._refresh_position_state(current_close)

        self._dates.append(trading_date)
        self._closes.append(current_close)
        self._raw_closes.append(current_close)
        self._volumes.append(float(bar.volume))
        self._trim_history()
        self._prepare_next_target(trading_date)

    def _submit_pending_target(self, trading_date: date, current_close: float) -> None:
        target_weight = self._pending_target_weight
        signal_date = self._pending_signal_date
        reason = self._pending_reason
        self._pending_target_weight = None
        self._pending_signal_date = None
        self._pending_reason = None

        if target_weight is None:
            return
        if trading_date < self._trade_start_date() or trading_date > self._trade_end_date():
            return

        instrument_id = str(self.config.instrument_id)
        self._record_target(
            trading_date=trading_date,
            signal_date=signal_date or trading_date,
            target_weight=target_weight,
            reason=reason or "macd_target",
        )
        self._submit_target_weight(
            trading_date=trading_date,
            instrument_id_text=instrument_id,
            close_price=current_close,
            target_weight=target_weight,
            reason=reason or "macd_target",
        )

    def _prepare_next_target(self, signal_date: date) -> None:
        min_required = max(
            self.config.long_period + self.config.signal_period + 2,
            self.config.entry_ma_period + 2,
            self.config.exit_ma_period + 2,
        )
        if len(self._closes) < min_required:
            return

        prices = self._closes[-self.config.observation :]
        raw_closes = self._raw_closes[-self.config.observation :]
        volumes = self._volumes[-self.config.observation :]
        macd_line, signal_line, hist = macd(
            prices,
            self.config.short_period,
            self.config.long_period,
            self.config.signal_period,
        )
        if not all(
            pd.notna(value)
            for value in (macd_line[-1], signal_line[-1], hist[-1], hist[-2])
        ):
            return

        latest_close = float(prices[-1])
        latest_raw_close = float(raw_closes[-1])
        latest_volume = float(volumes[-1])
        entry_ma = float(pd.Series(prices[-self.config.entry_ma_period :]).mean())
        exit_ma = float(pd.Series(prices[-self.config.exit_ma_period :]).mean())
        holding = self._current_quantity(self.config.instrument_id) > 0
        avg_price = self._entry_price or latest_raw_close
        stop_loss_triggered = (
            holding
            and avg_price > 0
            and latest_raw_close <= avg_price * (1.0 - float(self.config.stop_loss))
        )
        golden_cross = hist[-1] > 0 and hist[-2] < 0
        death_cross = hist[-1] < 0 and hist[-2] > 0
        entry_filter = macd_line[-1] > 0 and latest_close > entry_ma
        exit_filter = latest_close < exit_ma or stop_loss_triggered

        extra = {
            "macd": float(macd_line[-1]),
            "signal": float(signal_line[-1]),
            "hist": float(hist[-1]),
            "hist_prev": float(hist[-2]),
            "close": latest_close,
            "raw_close": latest_raw_close,
            "entry_ma": entry_ma,
            "exit_ma": exit_ma,
            "volume": latest_volume,
        }
        if golden_cross and entry_filter:
            self._set_pending_signal(
                signal_date=signal_date,
                target_weight=float(self.config.target_percent),
                score=float(hist[-1]),
                reason="macd_entry",
                extra=extra,
            )
            self.log.info(
                f"MACD entry signal {signal_date}: target={self.config.target_percent:.6f} "
                f"close={latest_close:.4f} hist={float(hist[-1]):.6f}",
                color=LogColor.GREEN,
            )
        elif holding and (death_cross or exit_filter):
            self._set_pending_signal(
                signal_date=signal_date,
                target_weight=0.0,
                score=float(hist[-1]),
                reason="macd_exit",
                extra={
                    **extra,
                    "death_cross": bool(death_cross),
                    "stop_loss_triggered": bool(stop_loss_triggered),
                    "avg_price": avg_price,
                    "quantity": int(self._current_quantity(self.config.instrument_id)),
                },
            )
            self.log.info(
                f"MACD exit signal {signal_date}: death_cross={death_cross} "
                f"stop_loss={stop_loss_triggered} close={latest_close:.4f}",
                color=LogColor.RED,
            )

    def _set_pending_signal(
        self,
        signal_date: date,
        target_weight: float,
        score: float,
        reason: str,
        extra: dict[str, Any],
    ) -> None:
        self._pending_target_weight = target_weight
        self._pending_signal_date = signal_date
        self._pending_reason = reason
        self.signal_events.append(
            MacdSmokeSignalEvent(
                signal_date=signal_date,
                instrument_id=str(self.config.instrument_id),
                signal_name="macd_target_percent",
                signal_value=target_weight,
                score=score,
                selected=True,
                reason=reason,
                extra=extra,
            ),
        )

    def _submit_target_weight(
        self,
        trading_date: date,
        instrument_id_text: str,
        close_price: float,
        target_weight: float,
        reason: str,
    ) -> bool:
        instrument_id = InstrumentId.from_str(instrument_id_text)
        instrument = self.cache.instrument(instrument_id)
        if instrument is None:
            self._record_order(
                trading_date,
                instrument_id_text,
                "buy",
                0,
                target_weight,
                "rejected",
                "missing_instrument",
            )
            self.log.error(f"Cannot submit target: missing instrument {instrument_id}")
            return False
        if close_price <= 0:
            self._record_order(
                trading_date,
                instrument_id_text,
                "buy",
                0,
                target_weight,
                "rejected",
                "missing_price",
            )
            return False

        current_qty = self._current_quantity(instrument_id)
        target_qty = self._target_quantity(instrument, close_price, target_weight)
        delta_qty = target_qty - current_qty
        if delta_qty == 0:
            self._record_order(
                trading_date,
                instrument_id_text,
                "buy",
                0,
                target_weight,
                "skipped",
                "already_target",
            )
            return True

        side = OrderSide.BUY if delta_qty > 0 else OrderSide.SELL
        qty_abs = abs(delta_qty)
        order = self.order_factory.market(
            instrument_id=instrument_id,
            order_side=side,
            quantity=instrument.make_qty(qty_abs),
        )
        self.submit_order(order)
        side_text = "buy" if side == OrderSide.BUY else "sell"
        if side == OrderSide.BUY and target_weight > 0:
            self._entry_price = close_price
        elif side == OrderSide.SELL and target_weight == 0:
            self._entry_price = None
        self._record_order(
            trading_date=trading_date,
            instrument_id=instrument_id_text,
            side=side_text,
            quantity=int(qty_abs),
            target_weight=target_weight,
            status="submitted",
            reason=reason,
            order_id=str(order.client_order_id),
        )
        self.log.info(
            f"Submitted {side_text} target {instrument_id_text} qty={qty_abs} "
            f"target_weight={target_weight:.6f}",
            color=LogColor.BLUE,
        )
        return True

    def _record_target(
        self,
        trading_date: date,
        signal_date: date,
        target_weight: float,
        reason: str,
    ) -> None:
        current_weight = self._current_weight(str(self.config.instrument_id))
        delta_weight = None if current_weight is None else target_weight - current_weight
        self.target_events.append(
            MacdSmokeTargetEvent(
                target_id=f"{signal_date.isoformat()}-{self.config.instrument_id}-macd_target_percent",
                target_date=signal_date,
                execute_date=trading_date,
                instrument_id=str(self.config.instrument_id),
                target_weight=float(target_weight),
                current_weight=current_weight,
                delta_weight=delta_weight,
                reason=reason,
                extra={"source_signal_name": "macd_target_percent"},
            ),
        )

    def _record_order(
        self,
        trading_date: date,
        instrument_id: str,
        side: str,
        quantity: int,
        target_weight: float,
        status: str,
        reason: str | None,
        order_id: str | None = None,
    ) -> None:
        self.order_events.append(
            MacdSmokeOrderEvent(
                order_id=order_id or f"internal-{trading_date.isoformat()}-{instrument_id}-{len(self.order_events)}",
                trading_date=trading_date,
                instrument_id=instrument_id,
                side=side,
                quantity=int(quantity),
                target_weight=float(target_weight),
                status=status,
                reason=reason,
                extra={"source": "strategy_internal"},
            ),
        )

    def _target_quantity(self, instrument: Any, close_price: float, target_weight: float) -> Decimal:
        if target_weight <= 0:
            return Decimal("0")
        portfolio_value = self._portfolio_value()
        raw_qty = portfolio_value * Decimal(str(target_weight)) / Decimal(str(close_price))
        lot_size = Decimal(str(instrument.lot_size))
        if lot_size <= 0:
            lot_size = Decimal("1")
        return (raw_qty // lot_size) * lot_size

    def _portfolio_value(self) -> Decimal:
        try:
            equity = self.portfolio.equity(venue=Venue(str(self.config.instrument_id.venue)))
        except Exception:
            equity = {}
        if equity:
            first = next(iter(equity.values()))
            try:
                return Decimal(str(first.as_decimal()))
            except Exception:
                return Decimal(str(float(first)))
        return Decimal(str(self.config.initial_cash))

    def _current_quantity(self, instrument_id: InstrumentId) -> Decimal:
        try:
            qty = self.portfolio.net_position(instrument_id)
        except Exception:
            return Decimal("0")
        if qty is None:
            return Decimal("0")
        return Decimal(str(qty))

    def _current_weight(self, instrument_id_text: str) -> float | None:
        close_price = self._raw_closes[-1] if self._raw_closes else None
        if close_price is None or close_price <= 0:
            return None
        portfolio_value = self._portfolio_value()
        if portfolio_value <= 0:
            return None
        qty = self._current_quantity(InstrumentId.from_str(instrument_id_text))
        return float(qty * Decimal(str(close_price)) / portfolio_value)

    def _refresh_position_state(self, current_close: float) -> None:
        current_qty = self._current_quantity(self.config.instrument_id)
        if current_qty <= 0:
            self._entry_price = None
        elif self._entry_price is None:
            self._entry_price = current_close

    def _trim_history(self) -> None:
        limit = max(int(self.config.observation), 1) + 10
        if len(self._closes) <= limit:
            return
        self._closes = self._closes[-limit:]
        self._raw_closes = self._raw_closes[-limit:]
        self._volumes = self._volumes[-limit:]
        self._dates = self._dates[-limit:]

    def _trade_start_date(self) -> date:
        return pd.Timestamp(self.config.trade_start_date).date()

    def _trade_end_date(self) -> date:
        return pd.Timestamp(self.config.trade_end_date).date()


def macd(
    values: list[float],
    short_period: int,
    long_period: int,
    signal_period: int,
) -> tuple[list[float], list[float], list[float]]:
    series = pd.Series(values, dtype="float64")
    short_ema = series.ewm(span=short_period, adjust=False, min_periods=short_period).mean()
    long_ema = series.ewm(span=long_period, adjust=False, min_periods=long_period).mean()
    macd_line = short_ema - long_ema
    signal = macd_line.ewm(span=signal_period, adjust=False, min_periods=signal_period).mean()
    hist = macd_line - signal
    return macd_line.to_list(), signal.to_list(), hist.to_list()


def bar_date(bar: Bar, timezone_name: str) -> date:
    return pd.Timestamp(bar.ts_event, unit="ns", tz="UTC").tz_convert(timezone_name).date()
