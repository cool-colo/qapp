from types import SimpleNamespace
from unittest.mock import MagicMock
from unittest.mock import patch

from lives.live_bigqmt_target_model_predictions import build_node


def test_bigqmt_node_uses_extended_connection_timeout() -> None:
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
        poll_interval_secs=1.0,
        adjust_type="none",
        strategy_name="target-model-predictions",
        no_sellable_check=False,
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
    assert venue_clients.timeout_connection_secs == 300.0
