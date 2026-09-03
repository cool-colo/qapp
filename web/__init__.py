"""Local read-only dashboard for live trading data.

FastAPI JSON backend (``web.app``) + static ECharts frontend (``web/static``)
that visualizes the ``live_*`` MySQL snapshot tables and ClickHouse daily bars.

Nothing in this package is imported by the strategy / backtest / live trading
code; it is a self-contained consumer of the data those paths persist.
"""
