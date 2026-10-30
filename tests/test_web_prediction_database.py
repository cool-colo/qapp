from __future__ import annotations

import pytest

from web.db import quote_qualified_table
from web.signal_quality_report import build_signal_quality_sql
from web.signal_series_report import build_signal_series_sql
from web.signal_series_report import build_snapshot_signals_sql
from web.signal_series_report import resolve_trade_signal_relations


def test_qualified_table_quotes_database_and_table() -> None:
    assert quote_qualified_table("quant_models", "daily_predictions") == (
        "`quant_models`.`daily_predictions`"
    )


def test_qualified_table_rejects_dotted_table_with_database() -> None:
    with pytest.raises(ValueError, match="database-qualified"):
        quote_qualified_table("quant_models", "other_db.daily_predictions")


def test_signal_series_sql_uses_prediction_database() -> None:
    sql = build_signal_series_sql(
        "quant_models",
        "daily_predictions",
        "000001.SZ",
        "2026-08-01",
        "2026-08-31",
    )

    assert "FROM `quant_models`.`daily_predictions`" in sql
    assert "FROM dwd_trade_calendar" in sql
    assert "min(c.cal_date) AS date" in sql
    assert "r.pred_date AS signal_date" in sql


def test_snapshot_signals_sql_uses_prediction_database() -> None:
    sql = build_snapshot_signals_sql(
        "quant_models",
        "daily_predictions",
        "2026-08-20",
    )

    assert "FROM `quant_models`.`daily_predictions`" in sql


def test_signal_quality_sql_uses_prediction_database() -> None:
    sql = build_signal_quality_sql(
        "quant_models",
        "daily_predictions",
        "2026-08-01",
        "2026-08-31",
        3,
    )

    assert "FROM `quant_models`.`daily_predictions`" in sql


def test_trade_signal_relation_prefers_explicit_order_lineage() -> None:
    trades = [{"trade_date": "2026-08-21", "order_signal_date": "2026-08-20"}]
    targets = [{"trade_date": "2026-08-21", "stock_code": "000001.SZ", "signal_date": "2026-08-19"}]
    series = [{"date": "2026-08-21", "signal_date": "2026-08-20"}]

    resolved = resolve_trade_signal_relations(trades, series, targets)

    assert resolved[0]["signal_date"] == "2026-08-20"
    assert resolved[0]["effect_date"] == "2026-08-21"
    assert resolved[0]["relation_status"] == "explicit"


def test_trade_signal_relation_uses_target_then_prior_signal_for_history() -> None:
    trades = [
        {"trade_date": "2026-08-21", "stock_code": "000001.SZ", "target_version": "v1"},
        {"trade_date": "2026-08-22", "stock_code": "000001.SZ"},
        {"trade_date": "2026-08-18", "stock_code": "000001.SZ"},
    ]
    series = [
        {"date": "2026-08-22", "signal_date": "2026-08-20"},
        {"date": "2026-08-25", "signal_date": "2026-08-21"},
    ]
    targets = [
        {
            "trade_date": "2026-08-21",
            "stock_code": "000001.SZ",
            "signal_date": "2026-08-20",
            "target_version": "v1",
        }
    ]

    resolved = resolve_trade_signal_relations(trades, series, targets)

    assert resolved[0]["signal_date"] == "2026-08-20"
    assert resolved[0]["effect_date"] == "2026-08-22"
    assert resolved[0]["relation_status"] == "target"
    assert resolved[1]["signal_date"] == "2026-08-21"
    assert resolved[1]["effect_date"] == "2026-08-25"
    assert resolved[1]["relation_status"] == "inferred"
    assert resolved[2]["signal_date"] is None
    assert resolved[2]["effect_date"] is None
    assert resolved[2]["relation_status"] == "unlinked"
