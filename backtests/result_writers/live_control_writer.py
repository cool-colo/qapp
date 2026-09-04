"""
MySQL persistence for manual trading-control state and the action audit log.

Mirrors the small engine/connection plumbing of ``LiveSnapshotWriter`` (a SQLAlchemy
QueuePool over pymysql, so concurrent callers on different threads never share a
socket). Two tables:

- ``live_control_state`` — the current pause flag per (account, trader), loaded at
  node startup so a manual pause survives a restart.
- ``live_control_action_log`` — an append-only audit trail of control actions
  (suspend / resume / sell / sell_all) with the request detail and result summary.

The writer is best-effort at the call sites: a MySQL outage must never block trading,
so callers wrap construction/loads in try/except and degrade to in-memory state.
"""

from __future__ import annotations

import json
from datetime import date
from datetime import datetime
from decimal import Decimal
from typing import Any
from typing import Mapping
from typing import Sequence


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


# Idempotent DDL, matching the style of live_writer.CREATE_TABLES_SQL.
CREATE_CONTROL_TABLES_SQL = (
    """
CREATE TABLE IF NOT EXISTS `live_control_state` (
  `id`             BIGINT       NOT NULL AUTO_INCREMENT,
  `account_id`     VARCHAR(64)  NOT NULL,
  `trader_id`      VARCHAR(64)  NOT NULL,
  `trading_paused` TINYINT(1)   NOT NULL DEFAULT 0,
  `updated_at`     DATETIME     NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_ctrl` (`account_id`,`trader_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
""",
    """
CREATE TABLE IF NOT EXISTS `live_control_action_log` (
  `id`          BIGINT       NOT NULL AUTO_INCREMENT,
  `account_id`  VARCHAR(64)  NOT NULL,
  `trader_id`   VARCHAR(64)  NOT NULL,
  `ts`          DATETIME     NOT NULL,
  `action`      VARCHAR(32)  NOT NULL,
  `detail_json` JSON         NULL,
  `result`      VARCHAR(1024) NULL,
  PRIMARY KEY (`id`),
  KEY `idx_acct_ts` (`account_id`,`trader_id`,`ts`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
""",
)


class LiveControlWriter:
    """Reads/writes the manual trading-control state + audit log in MySQL."""

    def __init__(
        self,
        engine=None,
        connect_kwargs: Mapping[str, Any] | None = None,
        commit: bool = True,
        create_tables: bool = True,
        logger: Any | None = None,
    ) -> None:
        self._logger = logger
        self._connect_kwargs = dict(connect_kwargs or {})
        self._engine = engine or self._create_engine(self._connect_kwargs)
        self._commit = commit
        if create_tables:
            self.create_tables()

    @classmethod
    def from_pymysql_kwargs(
        cls,
        *,
        logger: Any | None = None,
        create_tables: bool = True,
        **connect_kwargs: Any,
    ) -> "LiveControlWriter":
        return cls(
            connect_kwargs=connect_kwargs,
            logger=logger,
            create_tables=create_tables,
        )

    def set_logger(self, logger: Any) -> None:
        self._logger = logger

    @staticmethod
    def _create_engine(connect_kwargs: Mapping[str, Any]):
        try:
            import pymysql  # noqa: F401  (validates the driver is importable)
        except ImportError as exc:
            raise ImportError("pymysql is required to persist trading-control state") from exc
        try:
            from sqlalchemy import create_engine
            from sqlalchemy.pool import QueuePool
        except ImportError as exc:
            raise ImportError(
                "sqlalchemy is required to persist trading-control state"
            ) from exc

        kwargs = dict(connect_kwargs)

        def _creator():
            import pymysql

            return pymysql.connect(**kwargs)

        return create_engine(
            "mysql+pymysql://",
            creator=_creator,
            poolclass=QueuePool,
            pool_size=2,
            max_overflow=3,
            pool_pre_ping=True,
            pool_recycle=3600,
        )

    def close(self) -> None:
        dispose = getattr(self._engine, "dispose", None)
        if dispose is not None:
            dispose()
        self._engine = None

    def create_tables(self) -> None:
        for statement in CREATE_CONTROL_TABLES_SQL:
            self._execute(statement, ())

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------
    def load_trading_paused(self, account_id: str, trader_id: str) -> bool:
        rows = self._query(
            "SELECT `trading_paused` FROM `live_control_state` "
            "WHERE `account_id`=%s AND `trader_id`=%s",
            (account_id, trader_id),
        )
        if not rows:
            return False
        return bool(rows[0][0])

    def set_trading_paused(self, account_id: str, trader_id: str, paused: bool) -> None:
        self._execute(
            "INSERT INTO `live_control_state` "
            "(`account_id`,`trader_id`,`trading_paused`,`updated_at`) "
            "VALUES (%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE "
            "`trading_paused`=VALUES(`trading_paused`), `updated_at`=VALUES(`updated_at`)",
            (account_id, trader_id, 1 if paused else 0, datetime.now()),
        )

    # ------------------------------------------------------------------
    # Audit log
    # ------------------------------------------------------------------
    def append_action(
        self,
        account_id: str,
        trader_id: str,
        action: str,
        detail: Mapping[str, Any] | None,
        result: str | None,
    ) -> None:
        self._execute(
            "INSERT INTO `live_control_action_log` "
            "(`account_id`,`trader_id`,`ts`,`action`,`detail_json`,`result`) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (
                account_id,
                trader_id,
                datetime.now(),
                str(action),
                _json_dumps(detail),
                None if result is None else str(result)[:1024],
            ),
        )

    def load_recent_actions(
        self,
        account_id: str,
        trader_id: str,
        limit: int = 100,
    ) -> list[dict]:
        limit = max(1, min(int(limit), 1000))
        rows = self._query(
            "SELECT `ts`,`action`,`detail_json`,`result` FROM `live_control_action_log` "
            "WHERE `account_id`=%s AND `trader_id`=%s ORDER BY `ts` DESC LIMIT %s",
            (account_id, trader_id, limit),
        )
        actions: list[dict] = []
        for ts, action, detail_json, result in rows:
            detail: Any = None
            if detail_json is not None:
                if isinstance(detail_json, (dict, list)):
                    detail = detail_json
                else:
                    try:
                        detail = json.loads(detail_json)
                    except (TypeError, ValueError):
                        detail = None
            actions.append(
                {
                    "ts": ts.isoformat() if isinstance(ts, (date, datetime)) else str(ts),
                    "action": str(action),
                    "detail": detail,
                    "result": None if result is None else str(result),
                }
            )
        return actions

    # ------------------------------------------------------------------
    # Low-level DB helpers (mirror LiveSnapshotWriter)
    # ------------------------------------------------------------------
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
            connection.close()

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
