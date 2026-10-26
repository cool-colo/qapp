from __future__ import annotations

import pytest

from web.db import quote_qualified_table
from web.signal_quality_report import build_signal_quality_sql
from web.signal_series_report import build_signal_series_sql
from web.signal_series_report import build_snapshot_signals_sql


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
