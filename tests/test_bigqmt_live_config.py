import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from lives.broker_data_source import BigQmtBrokerDataSource
from lives.live_bigqmt_target_model_predictions import _apply_bigqmt_args
from lives.live_bigqmt_target_model_predictions import build_node


@pytest.mark.parametrize("tick_trace_enabled", [False, True])
def test_bigqmt_node_uses_extended_connection_timeout(tick_trace_enabled) -> None:
    args = SimpleNamespace(
        load_all_instruments=False,
        complete_instrument_details=False,
        restrict_reconciliation=False,
        account_id="12345678",
        account_type="STOCK",
        bigqmt_redis_host="redis.example.com",
        bigqmt_redis_port=6379,
        bigqmt_redis_db=5,
        bigqmt_redis_password="secret",
        transport="redis",
        rpc_timeout_secs=6.0,
        poll_interval_secs=3.0,
        adjust_type="none",
        strategy_name="target-model-predictions",
        no_sellable_check=False,
        bigqmt_account_cash_offset=250000.0,
        bigqmt_use_quote_push=False,
        bigqmt_tick_trace_enabled=tick_trace_enabled,
        bigqmt_market_poll_enabled=True,
        bigqmt_quote_push_stale_secs=30.0,
        bigqmt_quote_fallback_poll_interval_secs=60.0,
    )
    context = SimpleNamespace(instrument_ids=[])
    expected = object()

    with patch(
        "lives.live_bigqmt_target_model_predictions.build_target_model_node",
        return_value=expected,
    ) as builder:
        result = build_node(args, MagicMock())

    make_venue_clients = builder.call_args.args[2]
    venue_clients = make_venue_clients(args, context)

    assert result is expected
    assert venue_clients.timeout_connection_secs == 600.0
    data = venue_clients.data_client_config
    execution = venue_clients.exec_client_config
    assert data.transport == execution.transport == "redis"
    assert data.use_quote_push is False
    assert data.tick_trace_enabled is tick_trace_enabled
    assert data.poll_enabled is True
    assert data.poll_interval_secs == 3.0
    assert data.quote_push_stale_after_secs == 30.0
    assert data.quote_push_fallback_poll_interval_secs == 60.0
    assert execution.poll_enabled is True
    assert execution.poll_interval_secs == 1.0
    assert data.rpc_timeout_secs == execution.rpc_timeout_secs == 6.0
    assert execution.account_cash_offset == 250000.0
    add_recorders = builder.call_args.args[3]
    node = SimpleNamespace(trader=MagicMock())
    sources = tuple(object() for _ in range(6))
    with patch("lives.live_bigqmt_target_model_predictions._add_snapshot_recorder") as snapshots:
        add_recorders(args, node, context, *sources)
    snapshots.assert_called_once_with(args, node, context, *sources)
    assert node.trader.add_actor.call_count == int(tick_trace_enabled)
    if tick_trace_enabled:
        from lives.tick_trace_logging import BigQMTTickTraceLogger

        assert isinstance(node.trader.add_actor.call_args.args[0], BigQMTTickTraceLogger)


def test_bigqmt_cash_offset_comes_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("BIGQMT_ACCOUNT_CASH_OFFSET", "1250000.50")
    args = SimpleNamespace(account_id="12345678")

    _apply_bigqmt_args(args)

    assert args.bigqmt_account_cash_offset == 1250000.50


def test_bigqmt_quote_push_can_be_enabled_without_continuous_market_polling(monkeypatch) -> None:
    monkeypatch.setenv("BIGQMT_USE_QUOTE_PUSH", "1")
    args = SimpleNamespace(account_id="12345678")

    _apply_bigqmt_args(args)

    assert args.bigqmt_use_quote_push is True
    assert args.bigqmt_market_poll_enabled is False
    assert args.bigqmt_quote_push_stale_secs == 30.0
    assert args.bigqmt_quote_fallback_poll_interval_secs == 60.0


def test_bigqmt_order_snapshot_exposes_recorder_client_order_id() -> None:
    source = BigQmtBrokerDataSource(SimpleNamespace())
    source._query = AsyncMock(
        return_value=[
            {
                "stock_code": "600000.SH",
                "order_id": "venue-1",
                "order_remark": "O-20260805-001",
                "order_type": 23,
                "order_volume": 100,
                "traded_volume": 100,
                "order_status": 56,
            },
        ],
    )

    rows = asyncio.run(source.broker_order_snapshot())

    assert rows[0]["client_order_id"] == "O-20260805-001"
    assert rows[0]["instrument_id"] == "600000.SH.BIGQMT"
    assert rows[0]["lifecycle_status"] == "FILLED"


def test_bigqmt_order_snapshot_normalizes_qmt_lifecycle_statuses() -> None:
    source = BigQmtBrokerDataSource(SimpleNamespace())
    source._query = AsyncMock(
        return_value=[
            {
                "stock_code": "600000.SH",
                "order_remark": f"O-{status}",
                "order_status": status,
                "order_volume": 100,
                "traded_volume": traded_volume,
            }
            for status, traded_volume in (
                (50, 0),
                (55, 50),
                (53, 50),
                (54, 0),
                (56, 100),
                (57, 0),
                (255, 0),
            )
        ],
    )

    rows = asyncio.run(source.broker_order_snapshot())

    assert [row["lifecycle_status"] for row in rows] == [
        "ACCEPTED",
        "PARTIALLY_FILLED",
        "CANCELED",
        "CANCELED",
        "FILLED",
        "REJECTED",
        "UNKNOWN",
    ]


def test_bigqmt_trade_snapshot_exposes_recorder_trade_and_client_ids() -> None:
    source = BigQmtBrokerDataSource(SimpleNamespace())
    source._query = AsyncMock(
        return_value=[
            {
                "stock_code": "600000.SH",
                "trade_id": "trade-1",
                "order_remark": "O-20260805-001",
                "order_type": 23,
                "traded_volume": 100,
                "traded_price": 10.5,
            },
        ],
    )

    rows = asyncio.run(source.broker_trade_snapshot())

    assert rows[0]["traded_id"] == "trade-1"
    assert rows[0]["client_order_id"] == "O-20260805-001"
    assert rows[0]["instrument_id"] == "600000.SH.BIGQMT"


@pytest.mark.parametrize("value, expected", [("1", True), ("true", True), ("on", True), ("0", False), ("false", False), ("off", False)])
def test_tick_trace_env_boolean_is_applied(value, expected, monkeypatch):
    monkeypatch.setenv("BIGQMT_TICK_TRACE_ENABLED", value)
    args = SimpleNamespace(account_id="TRACE-TEST")
    _apply_bigqmt_args(args)
    assert args.bigqmt_tick_trace_enabled is expected


def test_tick_trace_defaults_off_and_rejects_invalid_env(monkeypatch):
    monkeypatch.delenv("BIGQMT_TICK_TRACE_ENABLED", raising=False)
    args = SimpleNamespace(account_id="TRACE-TEST")
    _apply_bigqmt_args(args)
    assert args.bigqmt_tick_trace_enabled is False
    monkeypatch.setenv("BIGQMT_TICK_TRACE_ENABLED", "invalid")
    with pytest.raises(ValueError, match="BIGQMT_TICK_TRACE_ENABLED must be a boolean"):
        _apply_bigqmt_args(args)


@pytest.mark.parametrize("env_value, cli_flags, expected", [
    (None, [], False), ("1", [], True), ("0", ["--bigqmt-tick-trace"], True),
    ("1", ["--no-bigqmt-tick-trace"], False),
    ("invalid", ["--bigqmt-tick-trace"], True),
])
def test_tick_trace_cli_overrides_env(env_value, cli_flags, expected, monkeypatch):
    import os
    import sys

    from lives import live_bigqmt_target_model_predictions as live

    with patch.dict(os.environ, {}, clear=True):
        if env_value is not None:
            monkeypatch.setenv("BIGQMT_TICK_TRACE_ENABLED", env_value)
        monkeypatch.setattr(live, "load_env", lambda *args, **kwargs: None)
        monkeypatch.setattr(sys, "argv", ["live", "--config", "configs/strategy.yaml", "--account-id", "TRACE-TEST", *cli_flags])
        args = live.parse_args()
    assert args.bigqmt_tick_trace_enabled is expected
    assert args.bigqmt_use_quote_push is False  # Enabling diagnostics does not switch feeds.


def test_tick_trace_options_are_in_cli_help(monkeypatch, capsys):
    import sys

    from lives import live_bigqmt_target_model_predictions as live

    monkeypatch.setattr(live, "load_env", lambda *args, **kwargs: None)
    monkeypatch.setattr(sys, "argv", ["live", "--help"])
    with pytest.raises(SystemExit) as exc:
        live.parse_args()
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "--bigqmt-tick-trace" in help_text
    assert "--no-bigqmt-tick-trace" in help_text
    assert "BIGQMT_TICK_TRACE_ENABLED" in help_text


def test_tick_trace_can_be_loaded_from_explicit_env_file(tmp_path, monkeypatch):
    import os
    import sys

    from lives import live_bigqmt_target_model_predictions as live

    env_file = tmp_path / "tick-trace.env"
    env_file.write_text("BIGQMT_TICK_TRACE_ENABLED=on\nQMT_ACCOUNT_ID=TRACE-TEST\n")
    with patch.dict(os.environ, {}, clear=True):
        monkeypatch.setattr(sys, "argv", ["live", "--config", "configs/strategy.yaml", "--env-file", str(env_file)])
        args = live.parse_args()
    assert args.bigqmt_tick_trace_enabled is True
