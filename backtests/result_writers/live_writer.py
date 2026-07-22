from __future__ import annotations

import json
from datetime import date
from datetime import datetime
from decimal import Decimal
from typing import Any
from typing import Iterable
from typing import Mapping
from typing import Sequence

from backtests.result_writers.live_records import AFTER_TRADING
from backtests.result_writers.live_records import BEFORE_TRADING
from backtests.result_writers.live_records import CONTINUOUS_TRADING
from backtests.result_writers.live_records import LiveAssetSnapshotRecord
from backtests.result_writers.live_records import LiveOrderRecord
from backtests.result_writers.live_records import LivePositionSnapshotRecord
from backtests.result_writers.live_records import LiveTargetRecord
from backtests.result_writers.live_records import LiveTradeRecord


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _json_dumps(value: Mapping[str, Any] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=_json_default)


def _timestamp(value: datetime | None) -> datetime:
    return value or datetime.now()


class _SingleConnectionEngine:
    """
    Minimal engine shim wrapping one already-open DB-API connection.

    Used only by ``LiveSnapshotWriter.for_testing`` so tests can inject a mock
    connection while the runtime code still goes through the real
    ``raw_connection()`` -> cursor -> close path. ``raw_connection`` hands back the
    same underlying connection every time; ``close()`` on it is intercepted so
    returning a connection to this "pool" does not close the mock, matching how a
    real pool recycles connections.
    """

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def raw_connection(self):
        return _NonClosingConnection(self._connection)

    def dispose(self) -> None:
        close = getattr(self._connection, "close", None)
        if close is not None:
            close()


class _NonClosingConnection:
    """Proxy that forwards everything to the wrapped connection but no-ops ``close``."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def close(self) -> None:  # returning to the "pool" must not close the mock
        return None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)



# DDL kept alongside the writer so a fresh deployment can bootstrap the schema.
# All statements are idempotent (IF NOT EXISTS) and match the frozen record shapes
# in live_records.py. QMT authoritative columns carry no prefix; Nautilus
# comparison columns carry the nt_ prefix.
CREATE_TABLES_SQL = (
    """
CREATE TABLE IF NOT EXISTS `live_asset_snapshot` (
  `id`            BIGINT       NOT NULL AUTO_INCREMENT,
  `trade_date`    DATE         NOT NULL,
  `write_time`    DATETIME     NOT NULL,
  `snapshot_type` VARCHAR(24)  NOT NULL,
  `account_id`    VARCHAR(64)  NOT NULL,
  `trader_id`     VARCHAR(64)  NOT NULL,
  `status`        VARCHAR(24)  NOT NULL DEFAULT 'ok',
  `total_asset`     DECIMAL(20,4) NULL,
  `market_value`    DECIMAL(20,4) NULL,
  `cash`            DECIMAL(20,4) NULL,
  `available_cash`  DECIMAL(20,4) NULL,
  `frozen_cash`     DECIMAL(20,4) NULL,
  `nt_equity`         DECIMAL(20,4) NULL,
  `nt_market_value`   DECIMAL(20,4) NULL,
  `nt_balance_total`  DECIMAL(20,4) NULL,
  `nt_balance_free`   DECIMAL(20,4) NULL,
  `nt_balance_locked` DECIMAL(20,4) NULL,
  `nt_unrealized_pnl` DECIMAL(20,4) NULL,
  `nt_realized_pnl`   DECIMAL(20,4) NULL,
  `source`        VARCHAR(24)  NOT NULL DEFAULT 'live',
  `qmt_raw`       JSON         NULL,
  `nt_raw`        JSON         NULL,
  `created_at`    DATETIME     NOT NULL,
  `schema_version` INT         NOT NULL DEFAULT 1,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_asset` (`account_id`,`trader_id`,`trade_date`,`snapshot_type`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
""",
    """
CREATE TABLE IF NOT EXISTS `live_position_snapshot` (
  `id`            BIGINT       NOT NULL AUTO_INCREMENT,
  `trade_date`    DATE         NOT NULL,
  `write_time`    DATETIME     NOT NULL,
  `snapshot_type` VARCHAR(24)  NOT NULL,
  `account_id`    VARCHAR(64)  NOT NULL,
  `trader_id`     VARCHAR(64)  NOT NULL,
  `status`        VARCHAR(24)  NOT NULL DEFAULT 'ok',
  `instrument_id` VARCHAR(32)  NOT NULL,
  `stock_code`    VARCHAR(16)  NOT NULL,
  `volume`         BIGINT       NULL,
  `can_use_volume` BIGINT       NULL,
  `avg_price`      DECIMAL(20,4) NULL,
  `open_price`     DECIMAL(20,4) NULL,
  `close_price`    DECIMAL(20,4) NULL,
  `market_value`   DECIMAL(20,4) NULL,
  `nt_net_qty`        BIGINT       NULL,
  `nt_avg_px_open`    DECIMAL(20,4) NULL,
  `nt_market_value`   DECIMAL(20,4) NULL,
  `nt_last_price`     DECIMAL(20,4) NULL,
  `nt_unrealized_pnl` DECIMAL(20,4) NULL,
  `source`        VARCHAR(24)  NOT NULL DEFAULT 'live',
  `qmt_raw`       JSON         NULL,
  `nt_raw`        JSON         NULL,
  `created_at`    DATETIME     NOT NULL,
  `schema_version` INT         NOT NULL DEFAULT 1,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_position` (`account_id`,`trader_id`,`trade_date`,`snapshot_type`,`instrument_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
""",
    """
CREATE TABLE IF NOT EXISTS `live_target_portfolio` (
  `id`            BIGINT       NOT NULL AUTO_INCREMENT,
  `trade_date`    DATE         NOT NULL,
  `write_time`    DATETIME     NOT NULL,
  `snapshot_type` VARCHAR(24)  NOT NULL,
  `account_id`    VARCHAR(64)  NOT NULL,
  `trader_id`     VARCHAR(64)  NOT NULL,
  `signal_date`   DATE         NULL,
  `asset_snapshot_id` BIGINT   NULL,
  `position_snapshot_id` BIGINT NULL,
  `total_asset`   DECIMAL(20,4) NULL,
  `investable_asset` DECIMAL(20,4) NULL,
  `request_id`    VARCHAR(128) NULL,
  `target_version` VARCHAR(128) NULL,
  `status`        VARCHAR(24)  NOT NULL DEFAULT 'ok',
  `instrument_id` VARCHAR(32)  NOT NULL,
  `stock_code`    VARCHAR(16)  NOT NULL,
  `target_weight` DECIMAL(12,8) NULL,
  `open_price`    DECIMAL(20,4) NULL,
  `price_source`  VARCHAR(16)  NULL,
  `target_qty`    BIGINT       NULL,
  `score`         DECIMAL(20,8) NULL,
  `expected_return` DECIMAL(20,8) NULL,
  `reason`        VARCHAR(64)  NULL,
  `extra`         JSON         NULL,
  `created_at`    DATETIME     NOT NULL,
  `schema_version` INT         NOT NULL DEFAULT 1,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_target` (`account_id`,`trader_id`,`trade_date`,`signal_date`,`snapshot_type`,`instrument_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
""",
    """
CREATE TABLE IF NOT EXISTS `live_order` (
  `id`            BIGINT       NOT NULL AUTO_INCREMENT,
  `trade_date`    DATE         NOT NULL,
  `write_time`    DATETIME     NOT NULL,
  `account_id`    VARCHAR(64)  NOT NULL,
  `trader_id`     VARCHAR(64)  NOT NULL,
  `client_order_id` VARCHAR(64) NOT NULL,
  `venue_order_id`  VARCHAR(64) NULL,
  `instrument_id` VARCHAR(32)  NOT NULL,
  `stock_code`    VARCHAR(16)  NULL,
  `side`          VARCHAR(8)   NULL,
  `source`        VARCHAR(16)  NOT NULL DEFAULT 'live',
  `order_type`    VARCHAR(16)  NULL,
  `limit_price`   DECIMAL(20,4) NULL,
  `quantity`      BIGINT       NULL,
  `filled_qty`    BIGINT       NULL DEFAULT 0,
  `avg_fill_price` DECIMAL(20,4) NULL,
  `status`        VARCHAR(24)  NOT NULL,
  `target_qty`    BIGINT       NULL,
  `target_version` VARCHAR(128) NULL,
  `open_price`    DECIMAL(20,4) NULL,
  `book_snapshot` JSON         NULL,
  `reason`        VARCHAR(64)  NULL,
  `qmt_raw`       JSON         NULL,
  `created_at`    DATETIME     NOT NULL,
  `updated_at`    DATETIME     NOT NULL,
  `schema_version` INT         NOT NULL DEFAULT 1,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_order` (`account_id`,`trader_id`,`client_order_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
""",
    """
CREATE TABLE IF NOT EXISTS `live_trade` (
  `id`            BIGINT       NOT NULL AUTO_INCREMENT,
  `trade_date`    DATE         NOT NULL,
  `write_time`    DATETIME     NOT NULL,
  `account_id`    VARCHAR(64)  NOT NULL,
  `trader_id`     VARCHAR(64)  NOT NULL,
  `trade_id`      VARCHAR(64)  NOT NULL,
  `client_order_id` VARCHAR(64) NOT NULL,
  `venue_order_id`  VARCHAR(64) NULL,
  `instrument_id` VARCHAR(32)  NOT NULL,
  `stock_code`    VARCHAR(16)  NULL,
  `side`          VARCHAR(8)   NULL,
  `source`        VARCHAR(16)  NOT NULL DEFAULT 'live',
  `price`         DECIMAL(20,4) NULL,
  `quantity`      BIGINT       NULL,
  `amount`        DECIMAL(20,4) NULL,
  `commission`    DECIMAL(20,4) NULL,
  `trade_time`    DATETIME     NULL,
  `qmt_raw`       JSON         NULL,
  `created_at`    DATETIME     NOT NULL,
  `schema_version` INT         NOT NULL DEFAULT 1,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_trade` (`account_id`,`trader_id`,`trade_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
""",
)


class LiveSnapshotWriter:
    """
    Persist live daily snapshots (assets, positions), frozen portfolio targets, and
    order/trade lifecycle events into the ``live_*`` MySQL schema.

    Kept independent of the backtest ``bt_*`` writer: the live tables carry a
    snapshot_type and account/trader identity rather than an experiment id, and hold
    both QMT authoritative fields (unprefixed) and Nautilus comparison fields (nt_).
    All writes use ``ON DUPLICATE KEY UPDATE`` so re-running before/after-trading is
    idempotent and order status updates overwrite prior rows.
    """

    def __init__(
        self,
        engine=None,
        connect_kwargs: Mapping[str, Any] | None = None,
        commit: bool = True,
        create_tables: bool = True,
        logger: Any | None = None,
    ) -> None:
        self._logger = logger
        # Keep the connect kwargs for reference/debugging. The engine below builds
        # each pooled connection from them via pymysql; the pool (not this class)
        # owns connection lifecycle and health.
        self._connect_kwargs = dict(connect_kwargs or {})
        # A SQLAlchemy engine wrapping a thread-safe QueuePool over pymysql. Every DB
        # op checks out its own connection and returns it, so concurrent callers on
        # different threads (e.g. a LiveClock timer thread vs. the node event loop)
        # never share a socket. This is what fixes the "Packet sequence number wrong"
        # protocol corruption that a single shared pymysql connection suffered under
        # concurrent access.
        self._engine = engine or self._create_engine(self._connect_kwargs)
        self._commit = commit
        if create_tables:
            self.create_tables()

    @classmethod
    def from_pymysql_kwargs(
        cls,
        *,
        logger: Any,
        **connect_kwargs: Any,
    ) -> "LiveSnapshotWriter":
        return cls(connect_kwargs=connect_kwargs, logger=logger)

    @classmethod
    def for_testing(
        cls,
        connection: Any,
        commit: bool = False,
        logger: Any | None = None,
    ) -> "LiveSnapshotWriter":
        """
        Build a writer around an already-constructed (mock) connection without touching
        a live DB. The connection is wrapped in a minimal single-connection engine shim
        so the runtime code path (``raw_connection()`` -> cursor -> close) is exercised
        exactly as it is against a real pool.
        """
        writer = cls.__new__(cls)
        writer._logger = logger
        writer._connect_kwargs = {}
        writer._engine = _SingleConnectionEngine(connection)
        writer._commit = commit
        return writer

    def set_logger(self, logger: Any) -> None:
        """Attach a runtime logger, typically the Nautilus node/actor logger."""
        self._logger = logger

    def _log_info(self, message: str, *args: Any) -> None:
        self._logger.info(message % args if args else message)

    def _log_warning(self, message: str, *args: Any) -> None:
        self._logger.warning(message % args if args else message)

    @staticmethod
    def _create_engine(connect_kwargs: Mapping[str, Any]):
        try:
            import pymysql  # noqa: F401  (validates the driver is importable)
        except ImportError as exc:
            raise ImportError("pymysql is required to write live snapshots to MySQL") from exc
        try:
            from sqlalchemy import create_engine
            from sqlalchemy.pool import QueuePool
        except ImportError as exc:
            raise ImportError(
                "sqlalchemy is required to write live snapshots to MySQL"
            ) from exc

        kwargs = dict(connect_kwargs)

        def _creator():
            import pymysql

            return pymysql.connect(**kwargs)

        # `mysql+pymysql://` only supplies the dialect (so pool_pre_ping can issue a
        # dialect-appropriate liveness check); the actual connection comes from
        # `_creator`, reusing the exact pymysql kwargs the caller passed. pool_pre_ping
        # transparently discards a connection the server closed while idle and opens a
        # fresh one on checkout, replacing the old hand-rolled reconnect logic.
        return create_engine(
            "mysql+pymysql://",
            creator=_creator,
            poolclass=QueuePool,
            pool_size=5,
            max_overflow=5,
            pool_pre_ping=True,
            pool_recycle=3600,
        )

    def close(self) -> None:
        dispose = getattr(self._engine, "dispose", None)
        if dispose is not None:
            dispose()
        self._engine = None

    def create_tables(self) -> None:
        for statement in CREATE_TABLES_SQL:
            self._execute(statement, ())
        self._ensure_target_columns()
        self._ensure_position_columns()
        self._ensure_order_columns()
        self._ensure_trade_columns()
        self._ensure_target_indexes()

    def _ensure_target_columns(self) -> None:
        """
        Add columns introduced after the table was first created. ``CREATE TABLE IF NOT
        EXISTS`` never alters an existing table, so older deployments miss the newer
        target columns; add them idempotently. Failures only warn (best-effort).
        """
        additions = {
            "position_snapshot_id": "BIGINT NULL",
            "investable_asset": "DECIMAL(20,4) NULL",
            "price_source": "VARCHAR(16) NULL",
            "expected_return": "DECIMAL(20,8) NULL",
        }
        try:
            existing = {
                str(row[0])
                for row in self._query("SHOW COLUMNS FROM `live_target_portfolio`", ())
            }
        except Exception:
            return
        for column, ddl in additions.items():
            if column in existing:
                continue
            try:
                self._execute(
                    f"ALTER TABLE `live_target_portfolio` ADD COLUMN `{column}` {ddl}",
                    (),
                )
            except Exception:
                # Concurrent add or insufficient privilege — leave as-is.
                pass

    def _ensure_position_columns(self) -> None:
        """
        Add position columns introduced after the initial live_position_snapshot
        deployment. Best-effort only.
        """
        additions = {
            "open_price": "DECIMAL(20,4) NULL",
            "close_price": "DECIMAL(20,4) NULL",
        }
        try:
            existing = {
                str(row[0])
                for row in self._query("SHOW COLUMNS FROM `live_position_snapshot`", ())
            }
        except Exception:
            return
        for column, ddl in additions.items():
            if column in existing:
                continue
            try:
                self._execute(
                    f"ALTER TABLE `live_position_snapshot` ADD COLUMN `{column}` {ddl}",
                    (),
                )
            except Exception:
                # Concurrent add or insufficient privilege — leave as-is.
                pass

    def _ensure_order_columns(self) -> None:
        """
        Add order columns introduced after the initial live_order deployment.
        Failures only warn implicitly via the caller's best-effort behavior.
        """
        additions = {
            "target_qty": "BIGINT NULL",
            "open_price": "DECIMAL(20,4) NULL",
            "book_snapshot": "JSON NULL",
            # Distinguishes QMT after-close backfilled rows (``fallback``) from live
            # msgbus rows (``live``).
            "source": "VARCHAR(16) NOT NULL DEFAULT 'live'",
        }
        try:
            existing = {
                str(row[0])
                for row in self._query("SHOW COLUMNS FROM `live_order`", ())
            }
        except Exception:
            return
        if "target_weight" in existing and "target_qty" not in existing:
            try:
                self._execute(
                    "ALTER TABLE `live_order` CHANGE COLUMN `target_weight` `target_qty` BIGINT NULL",
                    (),
                )
                existing.remove("target_weight")
                existing.add("target_qty")
            except Exception:
                # Fall back to the additive path below when rename/type-change is not allowed.
                pass
        for column, ddl in additions.items():
            if column in existing:
                continue
            try:
                self._execute(
                    f"ALTER TABLE `live_order` ADD COLUMN `{column}` {ddl}",
                    (),
                )
            except Exception:
                # Concurrent add or insufficient privilege — leave as-is.
                pass

    def _ensure_trade_columns(self) -> None:
        """
        Add trade columns introduced after the initial live_trade deployment.
        Best-effort only.
        """
        additions = {
            # See _ensure_order_columns: marks QMT-reconstructed rows.
            "source": "VARCHAR(16) NOT NULL DEFAULT 'live'",
        }
        try:
            existing = {
                str(row[0])
                for row in self._query("SHOW COLUMNS FROM `live_trade`", ())
            }
        except Exception:
            return
        for column, ddl in additions.items():
            if column in existing:
                continue
            try:
                self._execute(
                    f"ALTER TABLE `live_trade` ADD COLUMN `{column}` {ddl}",
                    (),
                )
            except Exception:
                # Concurrent add or insufficient privilege — leave as-is.
                pass

    def _ensure_target_indexes(self) -> None:
        """
        Keep the target table unique key aligned with the phase-aware write path.

        Early deployments keyed ``live_target_portfolio`` only by
        ``(account_id, trader_id, trade_date, signal_date, instrument_id)``, which lets
        a later ``continuous_trading`` write overwrite the earlier ``before_trading``
        row for the same instrument. The recorder and readers both treat snapshot phase
        as part of the identity, so include ``snapshot_type`` in ``uk_target``.
        """
        expected = (
            "account_id",
            "trader_id",
            "trade_date",
            "signal_date",
            "snapshot_type",
            "instrument_id",
        )
        try:
            rows = self._query("SHOW INDEX FROM `live_target_portfolio`", ())
        except Exception:
            return
        current = tuple(
            str(row[4])
            for row in sorted(
                (row for row in rows if len(row) >= 5 and str(row[2]) == "uk_target"),
                key=lambda row: int(row[3]),
            )
        )
        if current == expected:
            return
        try:
            if current:
                self._execute("ALTER TABLE `live_target_portfolio` DROP INDEX `uk_target`", ())
            self._execute(
                "ALTER TABLE `live_target_portfolio` "
                "ADD UNIQUE KEY `uk_target` "
                "(`account_id`,`trader_id`,`trade_date`,`signal_date`,`snapshot_type`,`instrument_id`)",
                (),
            )
        except Exception:
            # Concurrent DDL or insufficient privilege — leave as-is.
            pass

    # ---- writes --------------------------------------------------------------

    def write_asset_snapshot(self, record: LiveAssetSnapshotRecord) -> int:
        """Upsert one asset snapshot; return its row id (for target association)."""
        self._upsert_many(
            "live_asset_snapshot",
            [self._asset_row(record)],
            key_columns=("account_id", "trader_id", "trade_date", "snapshot_type"),
            preserve_columns=("created_at",),
        )
        row_id = self.asset_snapshot_id(
            record.account_id,
            record.trader_id,
            record.trade_date,
            record.snapshot_type,
        )
        return row_id or 0

    def write_position_snapshots(self, records: Sequence[LivePositionSnapshotRecord]) -> None:
        self._upsert_many(
            "live_position_snapshot",
            [self._position_row(record) for record in records],
            key_columns=(
                "account_id",
                "trader_id",
                "trade_date",
                "snapshot_type",
                "instrument_id",
            ),
            preserve_columns=("created_at",),
        )

    def write_target_portfolios(self, records: Sequence[LiveTargetRecord]) -> None:
        self._upsert_many(
            "live_target_portfolio",
            [self._target_row(record) for record in records],
            key_columns=(
                "account_id",
                "trader_id",
                "trade_date",
                "signal_date",
                "snapshot_type",
                "instrument_id",
            ),
            preserve_columns=("created_at",),
        )

    def upsert_order(self, record: LiveOrderRecord) -> None:
        self._upsert_many(
            "live_order",
            [self._order_row(record)],
            key_columns=("account_id", "trader_id", "client_order_id"),
            preserve_columns=("created_at",),
        )

    def upsert_trade(self, record: LiveTradeRecord) -> None:
        self._upsert_many(
            "live_trade",
            [self._trade_row(record)],
            key_columns=("account_id", "trader_id", "trade_id"),
            preserve_columns=("created_at",),
        )

    # ---- reads (restart-safe loading / idempotency) --------------------------

    def asset_snapshot_id(
        self,
        account_id: str,
        trader_id: str,
        trade_date: date,
        snapshot_type: str,
        fallback_to_continuous: bool = False,
    ) -> int | None:
        for snapshot_type_candidate in self._snapshot_type_candidates(
            snapshot_type,
            fallback_to_continuous=fallback_to_continuous,
        ):
            rows = self._query(
                "SELECT `id` FROM `live_asset_snapshot` "
                "WHERE `account_id`=%s AND `trader_id`=%s AND `trade_date`=%s AND `snapshot_type`=%s "
                "LIMIT 1",
                (account_id, trader_id, trade_date, snapshot_type_candidate),
            )
            if rows:
                return int(rows[0][0])
        return None

    def has_asset_snapshot(
        self,
        account_id: str,
        trader_id: str,
        trade_date: date,
        snapshot_type: str,
        fallback_to_continuous: bool = False,
    ) -> bool:
        return self.asset_snapshot_id(
            account_id,
            trader_id,
            trade_date,
            snapshot_type,
            fallback_to_continuous=fallback_to_continuous,
        ) is not None

    def has_position_snapshot(
        self,
        account_id: str,
        trader_id: str,
        trade_date: date,
        snapshot_type: str,
        fallback_to_continuous: bool = False,
    ) -> bool:
        for snapshot_type_candidate in self._snapshot_type_candidates(
            snapshot_type,
            fallback_to_continuous=fallback_to_continuous,
        ):
            rows = self._query(
                "SELECT 1 FROM `live_position_snapshot` "
                "WHERE `account_id`=%s AND `trader_id`=%s AND `trade_date`=%s AND `snapshot_type`=%s "
                "LIMIT 1",
                (account_id, trader_id, trade_date, snapshot_type_candidate),
            )
            if rows:
                return True
        return False

    def load_target_portfolios(
        self,
        account_id: str,
        trader_id: str,
        trade_date: date,
        signal_date: date | None,
        preferred_snapshot_type: str | None = None,
        fallback_to_continuous: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Load persisted target rows for the (account, trader, trade_date, signal_date)
        four-tuple. Returns dict rows (column name -> value) so callers can rebuild the
        frozen weights/quantities without a record round-trip. Empty list means "not
        generated yet — generate and persist a fresh target".
        """
        if signal_date is None:
            where = (
                "`account_id`=%s AND `trader_id`=%s AND `trade_date`=%s AND `signal_date` IS NULL"
            )
            params: tuple[Any, ...] = (account_id, trader_id, trade_date)
        else:
            where = (
                "`account_id`=%s AND `trader_id`=%s AND `trade_date`=%s AND `signal_date`=%s"
            )
            params = (account_id, trader_id, trade_date, signal_date)
        columns = [
            "instrument_id",
            "stock_code",
            "signal_date",
            "asset_snapshot_id",
            "position_snapshot_id",
            "total_asset",
            "investable_asset",
            "request_id",
            "target_version",
            "target_weight",
            "open_price",
            "price_source",
            "target_qty",
            "score",
            "expected_return",
            "reason",
            "snapshot_type",
        ]
        base_sql = f"SELECT {', '.join('`' + c + '`' for c in columns)} FROM `live_target_portfolio` "
        if preferred_snapshot_type is None:
            rows = self._query(f"{base_sql}WHERE {where}", params)
            return [dict(zip(columns, row)) for row in rows]
        for snapshot_type_candidate in self._snapshot_type_candidates(
            preferred_snapshot_type,
            fallback_to_continuous=fallback_to_continuous,
        ):
            rows = self._query(
                f"{base_sql}WHERE {where} AND `snapshot_type`=%s",
                params + (snapshot_type_candidate,),
            )
            if rows:
                return [dict(zip(columns, row)) for row in rows]
        return []

    def load_recent_target_dates(
        self,
        account_id: str,
        trader_id: str,
        trade_date: date,
        cutoff_trade_date: date,
        stock_codes: Sequence[str],
    ) -> dict[str, date]:
        """
        Resolve, per stock, the most recent ``trade_date`` that carried a positive
        target (``target_qty > 0``) within the window ``[cutoff_trade_date, trade_date)``
        — i.e. excluding today. ``before_trading`` rows win over other snapshot types on
        the same date. Returns ``{stock_code: recent_target_date}``; stocks with no
        qualifying row are absent.

        Used to populate the risk-manager request's ``recent_buy_date`` /
        ``recent_holding_days`` for currently-held positions.
        """
        codes = [str(code) for code in stock_codes if code]
        if not codes:
            return {}
        placeholders = ", ".join(["%s"] * len(codes))
        # Per (stock_code), take the latest trade_date; within a tie on date, prefer
        # before_trading (snapshot_type ordering weight 0) over other snapshot types (1).
        sql = (
            "SELECT `stock_code`, MAX(`trade_date`) AS recent_target_date "
            "FROM `live_target_portfolio` "
            "WHERE `account_id`=%s AND `trader_id`=%s "
            "AND `trade_date` >= %s AND `trade_date` < %s "
            "AND `target_qty` IS NOT NULL AND `target_qty` > 0 "
            f"AND `stock_code` IN ({placeholders}) "
            "GROUP BY `stock_code`"
        )
        params: tuple[Any, ...] = (
            account_id,
            trader_id,
            cutoff_trade_date,
            trade_date,
            *codes,
        )
        rows = self._query(sql, params)
        result: dict[str, date] = {}
        for row in rows:
            stock_code = str(row[0])
            recent = row[1]
            if recent is None:
                continue
            if isinstance(recent, datetime):
                recent = recent.date()
            elif not isinstance(recent, date):
                recent = datetime.strptime(str(recent), "%Y-%m-%d").date()
            result[stock_code] = recent
        return result

    def latest_asset_snapshot_value(
        self,
        account_id: str,
        trader_id: str,
        trade_date: date,
        prev_trade_date: date | None = None,
        column: str = "total_asset",
    ) -> Any:
        """
        Resolve a persisted asset value by input priority: today's before_trading →
        today's continuous_trading → the previous trading day's after_trading. Returns
        None when no snapshot exists at any tier.
        """
        column_sql = self._quote_identifier(column)
        for account, trader, day, snapshot_type in self._input_snapshot_tiers(
            account_id, trader_id, trade_date, prev_trade_date,
        ):
            rows = self._query(
                f"SELECT {column_sql} FROM `live_asset_snapshot` "
                "WHERE `account_id`=%s AND `trader_id`=%s AND `trade_date`=%s AND `snapshot_type`=%s "
                f"AND {column_sql} IS NOT NULL LIMIT 1",
                (account, trader, day, snapshot_type),
            )
            if rows and rows[0][0] is not None:
                return rows[0][0]
        return None

    def latest_position_snapshot_id(
        self,
        account_id: str,
        trader_id: str,
        trade_date: date,
        prev_trade_date: date | None = None,
    ) -> int | None:
        """
        Resolve a position-snapshot batch anchor id by the same input priority as
        ``latest_asset_snapshot_value``. The table stores one row per instrument with no
        batch column, so the smallest id for the winning (date, snapshot_type) group is
        returned as the anchor linking a target back to the positions it was built from.
        """
        for account, trader, day, snapshot_type in self._input_snapshot_tiers(
            account_id, trader_id, trade_date, prev_trade_date,
        ):
            rows = self._query(
                "SELECT MIN(`id`) FROM `live_position_snapshot` "
                "WHERE `account_id`=%s AND `trader_id`=%s AND `trade_date`=%s AND `snapshot_type`=%s",
                (account, trader, day, snapshot_type),
            )
            if rows and rows[0][0] is not None:
                return int(rows[0][0])
        return None

    @staticmethod
    def _input_snapshot_tiers(
        account_id: str,
        trader_id: str,
        trade_date: date,
        prev_trade_date: date | None,
    ) -> list[tuple[str, str, date, str]]:
        tiers = [
            (account_id, trader_id, trade_date, BEFORE_TRADING),
            (account_id, trader_id, trade_date, CONTINUOUS_TRADING),
        ]
        if prev_trade_date is not None:
            tiers.append((account_id, trader_id, prev_trade_date, AFTER_TRADING))
        return tiers

    @staticmethod
    def _snapshot_type_candidates(snapshot_type: str, fallback_to_continuous: bool) -> tuple[str, ...]:
        if fallback_to_continuous and snapshot_type == BEFORE_TRADING:
            return (BEFORE_TRADING, CONTINUOUS_TRADING)
        return (snapshot_type,)

    # ---- row builders --------------------------------------------------------

    @staticmethod
    def _asset_row(record: LiveAssetSnapshotRecord) -> dict[str, Any]:
        return {
            "trade_date": record.trade_date,
            "write_time": record.write_time,
            "snapshot_type": record.snapshot_type,
            "account_id": record.account_id,
            "trader_id": record.trader_id,
            "status": record.status,
            "total_asset": record.total_asset,
            "market_value": record.market_value,
            "cash": record.cash,
            "available_cash": record.available_cash,
            "frozen_cash": record.frozen_cash,
            "nt_equity": record.nt_equity,
            "nt_market_value": record.nt_market_value,
            "nt_balance_total": record.nt_balance_total,
            "nt_balance_free": record.nt_balance_free,
            "nt_balance_locked": record.nt_balance_locked,
            "nt_unrealized_pnl": record.nt_unrealized_pnl,
            "nt_realized_pnl": record.nt_realized_pnl,
            "source": record.source,
            "qmt_raw": _json_dumps(record.qmt_raw),
            "nt_raw": _json_dumps(record.nt_raw),
            "created_at": _timestamp(record.created_at),
            "schema_version": record.schema_version,
        }

    @staticmethod
    def _position_row(record: LivePositionSnapshotRecord) -> dict[str, Any]:
        return {
            "trade_date": record.trade_date,
            "write_time": record.write_time,
            "snapshot_type": record.snapshot_type,
            "account_id": record.account_id,
            "trader_id": record.trader_id,
            "status": record.status,
            "instrument_id": record.instrument_id,
            "stock_code": record.stock_code,
            "volume": record.volume,
            "can_use_volume": record.can_use_volume,
            "avg_price": record.avg_price,
            "open_price": record.open_price,
            "close_price": record.close_price,
            "market_value": record.market_value,
            "nt_net_qty": record.nt_net_qty,
            "nt_avg_px_open": record.nt_avg_px_open,
            "nt_market_value": record.nt_market_value,
            "nt_last_price": record.nt_last_price,
            "nt_unrealized_pnl": record.nt_unrealized_pnl,
            "source": record.source,
            "qmt_raw": _json_dumps(record.qmt_raw),
            "nt_raw": _json_dumps(record.nt_raw),
            "created_at": _timestamp(record.created_at),
            "schema_version": record.schema_version,
        }

    @staticmethod
    def _target_row(record: LiveTargetRecord) -> dict[str, Any]:
        return {
            "trade_date": record.trade_date,
            "write_time": record.write_time,
            "snapshot_type": record.snapshot_type,
            "account_id": record.account_id,
            "trader_id": record.trader_id,
            "signal_date": record.signal_date,
            "asset_snapshot_id": record.asset_snapshot_id,
            "position_snapshot_id": record.position_snapshot_id,
            "total_asset": record.total_asset,
            "investable_asset": record.investable_asset,
            "request_id": record.request_id,
            "target_version": record.target_version,
            "status": record.status,
            "instrument_id": record.instrument_id,
            "stock_code": record.stock_code,
            "target_weight": record.target_weight,
            "open_price": record.open_price,
            "price_source": record.price_source,
            "target_qty": record.target_qty,
            "score": record.score,
            "expected_return": record.expected_return,
            "reason": record.reason,
            "extra": _json_dumps(record.extra),
            "created_at": _timestamp(record.created_at),
            "schema_version": record.schema_version,
        }

    @staticmethod
    def _order_row(record: LiveOrderRecord) -> dict[str, Any]:
        return {
            "trade_date": record.trade_date,
            "write_time": record.write_time,
            "account_id": record.account_id,
            "trader_id": record.trader_id,
            "client_order_id": record.client_order_id,
            "venue_order_id": record.venue_order_id,
            "instrument_id": record.instrument_id,
            "stock_code": record.stock_code,
            "side": record.side,
            "source": record.source,
            "order_type": record.order_type,
            "limit_price": record.limit_price,
            "quantity": record.quantity,
            "filled_qty": record.filled_qty,
            "avg_fill_price": record.avg_fill_price,
            "status": record.status,
            "target_qty": record.target_qty,
            "target_version": record.target_version,
            "open_price": record.open_price,
            "book_snapshot": _json_dumps(record.book_snapshot),
            "reason": record.reason,
            "qmt_raw": _json_dumps(record.qmt_raw),
            "created_at": _timestamp(record.created_at),
            "updated_at": _timestamp(record.updated_at),
            "schema_version": record.schema_version,
        }

    @staticmethod
    def _trade_row(record: LiveTradeRecord) -> dict[str, Any]:
        return {
            "trade_date": record.trade_date,
            "write_time": record.write_time,
            "account_id": record.account_id,
            "trader_id": record.trader_id,
            "trade_id": record.trade_id,
            "client_order_id": record.client_order_id,
            "venue_order_id": record.venue_order_id,
            "instrument_id": record.instrument_id,
            "stock_code": record.stock_code,
            "side": record.side,
            "source": record.source,
            "price": record.price,
            "quantity": record.quantity,
            "amount": record.amount,
            "commission": record.commission,
            "trade_time": record.trade_time,
            "qmt_raw": _json_dumps(record.qmt_raw),
            "created_at": _timestamp(record.created_at),
            "schema_version": record.schema_version,
        }

    # ---- low-level SQL (mirrors MySQLResultWriter) ---------------------------

    def _upsert_many(
        self,
        table: str,
        rows: Sequence[Mapping[str, Any]],
        key_columns: Sequence[str],
        preserve_columns: Sequence[str] = (),
    ) -> None:
        if not rows:
            return
        columns = list(rows[0].keys())
        updates = [
            f"{self._quote_identifier(column)} = VALUES({self._quote_identifier(column)})"
            for column in columns
            if column not in set(key_columns).union(preserve_columns)
        ]
        sql = (
            f"INSERT INTO {self._quote_identifier(table)} "
            f"({', '.join(self._quote_identifier(column) for column in columns)}) "
            f"VALUES ({', '.join(['%s'] * len(columns))}) "
            f"ON DUPLICATE KEY UPDATE {', '.join(updates)}"
        )
        params = [tuple(row[column] for column in columns) for row in rows]
        self._executemany(sql, params)

    def _query(self, sql: str, params: Sequence[Any]) -> list[tuple[Any, ...]]:
        connection = self._engine.raw_connection()
        try:
            cursor = connection.cursor()
            try:
                cursor.execute(sql, params)
                return list(cursor.fetchall())
            finally:
                self._close_cursor(cursor)
        finally:
            connection.close()  # returns the connection to the pool

    def _execute(self, sql: str, params: Sequence[Any]) -> None:
        connection = self._engine.raw_connection()
        try:
            cursor = connection.cursor()
            try:
                cursor.execute(sql, params)
                self._commit_if_needed(connection)
            except Exception:
                self._rollback_if_needed(connection)
                raise
            finally:
                self._close_cursor(cursor)
        finally:
            connection.close()

    def _executemany(self, sql: str, params: Iterable[Sequence[Any]]) -> None:
        materialized = list(params)
        connection = self._engine.raw_connection()
        try:
            cursor = connection.cursor()
            try:
                cursor.executemany(sql, materialized)
                self._commit_if_needed(connection)
            except Exception:
                self._rollback_if_needed(connection)
                raise
            finally:
                self._close_cursor(cursor)
        finally:
            connection.close()

    def _commit_if_needed(self, connection: Any) -> None:
        if self._commit:
            connection.commit()

    def _rollback_if_needed(self, connection: Any) -> None:
        if not self._commit:
            return
        rollback = getattr(connection, "rollback", None)
        if rollback is not None:
            rollback()

    @staticmethod
    def _close_cursor(cursor) -> None:
        close = getattr(cursor, "close", None)
        if close is not None:
            close()

    @staticmethod
    def _quote_identifier(value: str) -> str:
        if not value.replace("_", "").isalnum():
            raise ValueError(f"Unsafe MySQL identifier: {value}")
        return f"`{value}`"
