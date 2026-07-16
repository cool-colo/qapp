# Fix MySQL "开盘大量失败" — 空闲连接被踢 + 重连不可用

## 根因

`LiveSnapshotWriter` 持有**单个** PyMySQL 连接(无连接池)。两个叠加问题:

1. **空闲连接被踢。** 交易时段之间进程长时间不写 MySQL,`wait_timeout` 到期后服务器
   关闭 socket。开盘一批 order event 同时涌入,第一次用死连接就报
   `(2013, 'Lost connection')` / `(0, '')`。

2. **重连不可用。** `from_pymysql_kwargs` 传入的 `connect_kwargs` 在 `__init__` 里用完
   即丢。`_reconnect()` 只能在已残废的连接对象上 `ping()`/`connect()`,PyMySQL 内部
   socket 已置 `None`,于是 `AttributeError("'NoneType' ... settimeout")`、`connect()`
   也失败。每个 order event 独立触发,开盘几十个订单 = 几十条失败日志。

## 设计原则(按用户要求)

**不用 `getattr` 兜底。** 需要的属性通过构造路径保证一定存在。当前逼出兜底的根源是
测试用 `object.__new__` 跳过 `__init__`——改为让测试走一个真正设置全部字段的构造入口。

## 修复

### 1. `backtests/result_writers/live_writer.py`

- `__init__` 新增 `self._connect_kwargs = dict(connect_kwargs or {})`,永久保存重连所需
  参数。外部直接传 `connection` 时它是 `{}`(表示"无法重建,只能 ping 旧连接")。
- 新增**测试用类方法** `for_testing(cls, connection, commit=False)`:一步设好
  `_connection`/`_commit`/`_connect_kwargs`(以及其它 `__init__` 会设的字段),
  `create_tables=False`。测试改用它,不再 `object.__new__`。这样运行代码里
  `self._connect_kwargs` 永远存在,可直接访问,无需 `getattr`。
- `_reconnect()` 重写为分层策略,直接读 `self._connect_kwargs`:
  1. 若有 `_connect_kwargs` → **丢弃旧连接、`self._connection =
     self._create_connection(self._connect_kwargs)` 全新建连**(这是唯一可靠恢复路径);
  2. 否则(外部注入的连接,无 kwargs)→ 退回 `ping(reconnect=True)`,再退回
     `connection.connect()`。
  失败记 warning 并返回 False,保持现有日志语义。
- 新增公开 `ping() -> bool`:走 `_with_reconnect` 执行 `SELECT 1`;失败 warning 不抛。
  供保活定时器调用。

### 2. `lives/snapshot_recorder.py`

- `SnapshotRecorderConfig` 新增 `keepalive_secs: int = 20`(20 秒)。
- `on_start()` 用 `self.clock.set_timer(name="SNAPSHOT-DB-KEEPALIVE",
  interval=timedelta(seconds=keepalive_secs), callback=self._on_keepalive,
  fire_immediately=False)` 注册。
- `_on_keepalive` 调 `self._writer.ping()`,`try/except` 包裹,失败只 warning。

### 3. `tests/test_target_live_config.py`

- 把重连相关的 5 处 `object.__new__(LiveSnapshotWriter)` + 手设 `_connection` 改成
  `LiveSnapshotWriter.for_testing(connection=connection, commit=...)`,让 `_connect_kwargs`
  存在(设为 `{}`,从而重连测试仍走 `ping`/`connect` 分支,断言 `ping` 被调不变)。
- 其余非重连相关的 `object.__new__` 用法可保留(它们不碰 `_connect_kwargs`),或一并
  切到 `for_testing` 保持一致——实现时统一切换更干净。
- 新增测试:`_connect_kwargs` 非空时重连走 `_create_connection` 全新建连(patch 掉
  `pymysql.connect` / `_create_connection`,断言旧连接被替换)。
- 新增测试:`ping()` 成功/失败路径。
- 新增测试:recorder `on_start` 注册 keepalive 定时器、`_on_keepalive` 调 `writer.ping`。

## 影响

- 保活 ping 解决 99% 的"开盘首次失败";重连改成全新建连兜住偶发断连。
- 交易逻辑零改动(strategy 仍 DB-free),只动 writer + recorder actor + 测试。
- 手动验证:`python lives/live_qmt_target_model_predictions.py --build-only ...` 构建通过;
  跑 `tests/test_target_live_config.py` 全绿。

## 不做

- 不引入连接池(单 writer actor 串行写,无并发需求)。
- 不改 strategy / 交易路径。
- 代码里不使用 `getattr` 兜底缺失属性。
