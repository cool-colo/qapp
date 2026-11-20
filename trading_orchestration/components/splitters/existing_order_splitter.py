from strategies.order_splitter.notional_order_splitter import NotionalOrderSplitter


class ExistingOrderSplitter:
    """Adapt the established quantity rules and notional slicing algorithm."""

    def __init__(self, max_order_notional):
        self.delegate = NotionalOrderSplitter(max_order_notional)

    def split(self, instrument, quantity, price, *, allow_odd_lot=False):
        return self.delegate.split(instrument, quantity, price, allow_odd_lot=allow_odd_lot)
