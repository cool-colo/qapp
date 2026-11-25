from unittest.mock import MagicMock, patch

from web.app import get_asset_series, get_positions


def test_snapshot_all_queries_do_not_default_to_after_trading():
    data = MagicMock()
    mysql = data.mysql.return_value
    mysql.query.return_value = []
    data.attach_names.return_value = []
    scope = dict(source="test", account="A", trader="T", data=data)
    get_asset_series(**scope, start="2026-11-25", end="2026-11-25", snapshot_type="all")
    sql, params = mysql.query.call_args.args
    assert "%(snapshot_type)s = 'all' OR" in sql
    assert params["snapshot_type"] == "all"
    with patch("web.app._position_classifications", return_value={}):
        get_positions(**scope, date="2026-11-25", snapshot_type="all")
    sql, params = mysql.query.call_args.args
    assert "snapshot_type =" not in sql
    assert "snapshot_type" not in params


def test_before_auction_assets_are_queryable():
    data = MagicMock()
    data.mysql.return_value.query.return_value = []
    get_asset_series(source="test", account="A", trader="T", start="2026-11-25",
                     end="2026-11-25", snapshot_type="before_auction", data=data)
    assert data.mysql.return_value.query.call_args.args[1]["snapshot_type"] == "before_auction"
