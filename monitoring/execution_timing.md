# 交易执行耗时日志

每轮收敛结束后，策略通过现有 Nautilus logger 输出一条 INFO 日志，以
`live_convergence ` 开头，后面是单行 JSON。`live_convergence` 只是日志标识，
不创建或写入同名 MySQL 表，也不进入下单决策的数据库写入队列。

正常执行、没有下单、窗口/暂停/锁/目标状态导致的跳过、异常和任务取消都会记录。
不采样，不修改收敛周期、撤单延迟或交易规则；不测量启动、`sleep(10)` 或 risk manager。
收敛记录不额外保留内存历史，也没有用于落库的订阅/重放机制。

下单决策仍通过 `SnapshotRecorder` 写入现有 MySQL `live_order_decision`。
日志的 `convergence_id` 与该表 `context.execution_timing.convergence_id` 相同，
可人工关联整轮收敛和每笔下单决策。日志还包括 `strategy_id` 和 `trading_date`。

## 时间口径

`*_ns` 单位都是纳秒，需要区分绝对时间与耗时：

- `requested_ns`、`started_ns`、`execution_started_ns`、`finished_ns` 和分段
  `timestamp_ns` 使用 Nautilus 策略时钟。实盘是 Unix 时间，可与 `decision_ns`、
  行情 `ts_init` 对齐；回测是模拟时钟。
- `*_duration_ns`、`delay_elapsed_ns`、`observed_to_capture_ns` 和分段 `elapsed_ns`
  使用 `perf_counter_ns()` 的差值，表示本进程实测耗时，不受系统校时影响。
  回测时表示运行代码的耗时，不能当作模拟成交延迟。
- `ts_event` 和 `ts_init` 原样保留。绝对时间相减可能受源时钟偏差或系统校时影响，
  不代替单调时钟耗时；不会把负差值裁剪成零。
- 未达到的阶段为 JSON null 或不出现相应字段，不填零。例如启动前取消没有
  `started_ns`；盘口日期不合法时没有用于盘口年龄计算的 `validation_ns`。

## 每轮收敛的日志字段

| 字段 | 含义 |
| --- | --- |
| `convergence_id` | 每轮 UUID，关联下单决策 |
| `trigger` | timer、卖出成交、撤单、盘口、bar、竞价或手工控制等触发来源 |
| `status` / `reason` | completed / skipped / failed / cancelled，以及具体原因 |
| `requested_ns` | 投递协程前；同步路径为进入收敛入口时 |
| `started_ns` | 协程开始运行；同步路径紧接请求开始 |
| `execution_started_ns` | 主动延迟结束，进入同步收敛，开始检查窗口和锁 |
| `finished_ns` | 本轮结束，包含提前返回或异常；在格式化日志之前捕获 |
| `queue_duration_ns` | requested → started，初次等待事件循环 |
| `configured_delay_ns` | 请求的撤单结算延迟；其他触发为零 |
| `delay_elapsed_ns` | started → execution_started，包含主动延迟和到期后等待恢复 |
| `execution_duration_ns` | execution_started → finished，包含窗口检查、等待锁和实际工作 |
| `total_duration_ns` | requested → finished，整轮实际耗时 |
| `pending_at_request` | 请求时尚未启动的异步收敛数；异步请求包含自己，同步调用不计自己 |
| `pending_at_start` | 此任务启动后，其他尚未启动的异步收敛数；不含正在主动延迟的任务 |

成功到达执行入口的任务满足：

```text
total_duration_ns = queue_duration_ns + delay_elapsed_ns + execution_duration_ns
```

`delay_elapsed_ns - configured_delay_ns` 可观察主动延迟到期后的恢复拖延，不能将
整段 delay 都算成初次排队。无主动延迟时这一段仍有少量调用开销。

`context.timer_event_ns` 保存定时器原始 `ts_event`，可与 `requested_ns` 对比。
`context.trigger_ts_event` 保存同步调用传入的行情触发时间。
`context.target_version` 表示执行入口看到的目标版本。

`context.stages.<名称>` 保存 `timestamp_ns` 和从 requested 开始累计的
`elapsed_ns`。两个阶段的 `elapsed_ns` 相减，得到该分段的单调耗时：

| 阶段 | 已完成的工作 |
| --- | --- |
| `execution_started` | 开始同步收敛 |
| `window_checked` | 执行/撤单窗口检查 |
| `lock_acquired` | 获得收敛锁；与上一阶段之差可发现手工控制等路径的锁等待 |
| `cancel_check_finished` | 检查已有订单是否需要撤单 |
| `account_scan_started` / `account_scan_finished` | 账户日志、未完成订单及目标/持仓扫描 |
| `intents_finished` | 全部标的意图构建、定价和分类完成 |
| `summary_finished` | 汇总日志及其现金/订单查询完成 |
| `sells_finished` / `buys_finished` | 连续交易卖单/买单处理完成，包含现金门控和逐笔提交 |
| `auction_submit_finished` | 集合竞价订单处理完成；与连续交易阶段互斥 |
| `achievement_check_finished` | 检查目标是否已经达成 |
| `finished` | 返回或异常退出 |

`context` 还记录 `desired_count`、`sell_intent_count`、`buy_intent_count`。
异常只保存 `error_type`，不额外记录完整委托、盘口或账户载荷。

## 每笔下单决策的数据库字段

连续交易的 `live_order_decision.context.price_context` 新增：

| 字段 | 含义 |
| --- | --- |
| `ts_event` / `ts_init` | 原始行情时间，保持原语义 |
| `observed_ns` | 这份盘口到达策略行情回调入口的时间 |
| `captured_ns` | 本次定价取出该盘口快照的时间 |
| `observed_to_capture_ns` | 回调入口 → 取出盘口，用单调时钟计算的缓存停留时间 |
| `book_source` | depth / quote / missing |

只有实际匹配的 source event/init 时间对才提供 `observed_ns`。如果报价仅存在于
Nautilus cache、策略没有收到匹配回调，就保留 null，不用取出时刻冒充收到时刻。

`context.execution_timing` 中保存：

- 当前轮次的 `convergence_id`、`convergence_trigger`、`requested_ns`、`started_ns`、
  `execution_started_ns`。
- `intent_started_ns`、`intent_finished_ns` / `intent_duration_ns`：构建单个标的意图。
- `book_context_started_ns`、`book_context_finished_ns` / `book_context_duration_ns`：
  读取盘口、转换数量单位及计算趋势输入。
- `validation_started_ns`、`validation_ns`、`validation_finished_ns` /
  `validation_duration_ns`：盘口检查。`validation_ns` 是原 `book_age_ms` 计算使用的时刻。
- `pricing_started_ns`、`pricing_finished_ns` / `pricing_duration_ns`：价格计算、涨跌停钳制
  和价格精度转换。
- `slice_started_ns`、`order_created_ns` / `slice_prepare_duration_ns`：这一拆单的准备，
  包含卖出可用数量检查和订单工厂构建。
- 集合竞价使用原有 `reference_ts_event`、`reference_ts_init`，另记录
  `reference_captured_ns` / `reference_to_intent_ns` 和竞价 `pricing_finished_ns`。
  参考价捕获是策略接受参考价的位置，不等于行情回调入口；不套用连续交易的盘口检查阶段。

同一轮先准备意图、后批量提交。`slice_started_ns - intent_finished_ns` 可观察该笔拆单
开始前的等待。每笔决策保存独立时间快照，后续拆单不会覆盖此前已发出的时间。

实际 `submit_order()` 起止时间在收敛日志的 `context.submissions[]` 中，包含
`client_order_id`、`decision_id`、`slice_index`、`slice_count`、`submit_started_ns`、
`submit_finished_ns`、`submit_duration_ns` 和 `decision_emit_duration_ns`。
成功返回为 `status=returned`，调用异常为 `failed`；失败报告时间不混入调用耗时。
这里只测 Nautilus 本地调用；到券商确认、订单接受、成交的延迟继续看 `live_order_event`。

## 人工关联

先从 MySQL 查某日的下单决策及收敛 ID（账户、交易者和日期按需替换）：

```sql
SELECT decision_id, client_order_id, decision_ns,
       JSON_UNQUOTE(JSON_EXTRACT(context, '$.execution_timing.convergence_id')) AS convergence_id,
       context
FROM live_order_decision
WHERE trade_date = '2026-11-13'
  AND account_id = 'ACCOUNT' AND trader_id = 'TRADER'
  AND decision_type IN ('SUBMIT_REQUESTED', 'REPLACEMENT_SUBMIT')
ORDER BY decision_ns;
```

再用得到的 ID 搜索日志：

```bash
rg -F '"convergence_id":"CONVERGENCE_ID"' logs/
```

反向关联时，在上面的 SQL 中添加：

```sql
AND JSON_UNQUOTE(JSON_EXTRACT(context, '$.execution_timing.convergence_id')) = 'CONVERGENCE_ID'
```

该条件加在 `ORDER BY` 之前。一轮可能对应多个标的、多笔拆单；日志中的 `submissions[]`
可继续按 `decision_id` 或 `client_order_id` 匹配到具体委托。

`ts_init → observed_ns → captured_ns → decision_ns` 覆盖行情生命周期；
queue/delay/execution 覆盖收敛生命周期，两者可能重叠，不能再次相加。
按需分别分析，缓存停留的单调耗时直接读取 `observed_to_capture_ns`。

日志沿用节点现有日志级别、文件路径和轮转配置，需要开启 INFO。
默认 0.5 秒的定时器会输出约每秒两条收敛日志，其他触发另计。
取时、构造诊断和 JSON 格式化仍有 CPU 开销；收敛日志不会增加 MySQL 写入或数据库队列积压。
日志本身的格式化/输出开销在本轮 `finished_ns` 之后，不计入本轮执行耗时。
