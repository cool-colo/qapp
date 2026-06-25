from decimal import Decimal

from nautilus_trader.config import StrategyConfig
from nautilus_trader.common.enums import LogColor
from nautilus_trader.indicators import ExponentialMovingAverage
from nautilus_trader.model.data import Bar
from nautilus_trader.model.data import BarType
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy


class EMACrossConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType
    trade_size: Decimal
    fast_ema_period: int = 10
    slow_ema_period: int = 20
    allow_short: bool = False
    flatten_on_stop: bool = False
    log_quote_ticks: bool = False


class EMACross(Strategy):
    def __init__(self, config: EMACrossConfig):
        super().__init__(config)
        self.fast_ema = ExponentialMovingAverage(config.fast_ema_period)
        self.slow_ema = ExponentialMovingAverage(config.slow_ema_period)

    def on_start(self):
        self.register_indicator_for_bars(self.config.bar_type, self.fast_ema)
        self.register_indicator_for_bars(self.config.bar_type, self.slow_ema)
        self.subscribe_quote_ticks(self.config.instrument_id)
        self.subscribe_bars(self.config.bar_type)

    def on_quote_tick(self, tick: QuoteTick):
        if not self.config.log_quote_ticks:
            return

        self.log.info(
            "Quote tick, "
            f"instrument_id={tick.instrument_id}, "
            f"bid_price={tick.bid_price}, bid_size={tick.bid_size}, "
            f"ask_price={tick.ask_price}, ask_size={tick.ask_size}, "
            f"ts_event={tick.ts_event}, ts_init={tick.ts_init}",
            color=LogColor.CYAN,
        )

    def on_bar(self, bar: Bar):
        if not self.indicators_initialized():
            self.log.info(
                f"Bar received while warming indicators: {bar}",
                color=LogColor.BLUE,
            )
            return

        self.log.info(
            f"Bar, bar={bar}, close={bar.close} fast_ema={self.fast_ema.value:.4f} slow_ema={self.slow_ema.value:.4f}",
            color=LogColor.BLUE,
        )

        open_orders = self.cache.orders_open_count(instrument_id=self.config.instrument_id)
        if open_orders:
            self.log.info(
                f"Signal skipped: {open_orders} open order(s) for {self.config.instrument_id}",
                color=LogColor.BLUE,
            )
            return

        if self.fast_ema.value >= self.slow_ema.value:
            if self.portfolio.is_net_short(self.config.instrument_id):
                self.log.info("Signal BUY: closing short before evaluating long entry", color=LogColor.GREEN)
                self.close_all_positions(self.config.instrument_id)
                return
            if self.portfolio.is_flat(self.config.instrument_id):
                self.log.info("Signal BUY: fast EMA >= slow EMA and portfolio is flat", color=LogColor.GREEN)
                self.buy()
        elif self.fast_ema.value < self.slow_ema.value:
            if self.portfolio.is_net_long(self.config.instrument_id):
                self.log.info("Signal SELL: closing long before evaluating short entry", color=LogColor.RED)
                self.close_all_positions(self.config.instrument_id)
                return
            if self.portfolio.is_flat(self.config.instrument_id):
                if not self.config.allow_short:
                    self.log.info("Signal SELL ignored: strategy is long-only", color=LogColor.BLUE)
                    return
                self.log.info("Signal SELL: fast EMA < slow EMA and portfolio is flat", color=LogColor.RED)
                self.sell()

    def buy(self):
        instrument = self.cache.instrument(self.config.instrument_id)
        if instrument is None:
            self.log.error(f"Cannot submit BUY: missing instrument {self.config.instrument_id}")
            return
        order = self.order_factory.market(
            self.config.instrument_id,
            OrderSide.BUY,
            instrument.make_qty(self.config.trade_size),
        )
        self.submit_order(order)

    def sell(self):
        instrument = self.cache.instrument(self.config.instrument_id)
        if instrument is None:
            self.log.error(f"Cannot submit SELL: missing instrument {self.config.instrument_id}")
            return
        order = self.order_factory.market(
            self.config.instrument_id,
            OrderSide.SELL,
            instrument.make_qty(self.config.trade_size),
        )
        self.submit_order(order)

    def on_stop(self):
        if not self.config.flatten_on_stop:
            self.log.info("Stop received: leaving positions/orders unchanged", color=LogColor.BLUE)
            return

        open_orders = self.cache.orders_open_count(instrument_id=self.config.instrument_id)
        if open_orders:
            self.log.warning(
                f"Stop flatten skipped: {open_orders} open order(s) for {self.config.instrument_id}",
            )
            return

        self.log.info("Stop flatten enabled: closing open positions", color=LogColor.RED)
        self.close_all_positions(self.config.instrument_id)
