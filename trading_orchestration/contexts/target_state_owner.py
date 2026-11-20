"""Named accessors preserve the existing executor API over one state owner."""


class TargetStateOwner:

    @property
    def _market_status(self):
        return self.trading_state.market.market_status

    @_market_status.setter
    def _market_status(self, value):
        self.trading_state.market.market_status = value

    @property
    def _whole_market_status(self):
        return self.trading_state.market.whole_market_status

    @_whole_market_status.setter
    def _whole_market_status(self, value):
        self.trading_state.market.whole_market_status = value

    @property
    def _depth_books(self):
        return self.trading_state.market.depth_books

    @_depth_books.setter
    def _depth_books(self, value):
        self.trading_state.market.depth_books = value

    @property
    def _today_open(self):
        return self.trading_state.market.today_open

    @_today_open.setter
    def _today_open(self, value):
        self.trading_state.market.today_open = value

    @property
    def _authoritative_open(self):
        return self.trading_state.market.authoritative_open

    @_authoritative_open.setter
    def _authoritative_open(self, value):
        self.trading_state.market.authoritative_open = value

    @property
    def _last_close(self):
        return self.trading_state.market.last_close

    @_last_close.setter
    def _last_close(self, value):
        self.trading_state.market.last_close = value

    @property
    def _target_quantities(self):
        return self.trading_state.execution.target_quantities

    @_target_quantities.setter
    def _target_quantities(self, value):
        self.trading_state.execution.target_quantities = value

    @property
    def _frozen_instruments(self):
        return self.trading_state.execution.frozen_instruments

    @_frozen_instruments.setter
    def _frozen_instruments(self, value):
        self.trading_state.execution.frozen_instruments = value

    @property
    def _deferred_buys(self):
        return self.trading_state.execution.deferred_buys

    @_deferred_buys.setter
    def _deferred_buys(self, value):
        self.trading_state.execution.deferred_buys = value

    @property
    def _insufficient_funds(self):
        return self.trading_state.execution.insufficient_funds

    @_insufficient_funds.setter
    def _insufficient_funds(self, value):
        self.trading_state.execution.insufficient_funds = value

    @property
    def _pending_replacements(self):
        return self.trading_state.execution.pending_replacements

    @_pending_replacements.setter
    def _pending_replacements(self, value):
        self.trading_state.execution.pending_replacements = value

    @property
    def _order_quote_states(self):
        return self.trading_state.execution.order_quote_states

    @_order_quote_states.setter
    def _order_quote_states(self, value):
        self.trading_state.execution.order_quote_states = value

