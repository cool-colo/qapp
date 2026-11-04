# Nautilus Tick 与五档盘口回测撮合机制

## 目标

本文说明如何在本项目使用 Nautilus Trader 回放真实 tick 和五档盘口，并解释模拟交易所
如何决定订单成交、部分成交、继续挂单或撤单。该方案不刻意构造特定行情，而是让策略
订单自然地与按时间顺序回放的历史市场数据交互。

本文讨论的主要数据类型为：

- `OrderBookDepth10`：完整十档盘口快照；只有五档数据时，Nautilus 自动将其余档位补空；
- `TradeTick`：真实成交价、成交量和主动买卖方向；
- 策略 full-tick 事件：最新价和累计成交量，用于当前价格、成交量趋势计算。

## 撮合引擎维护的状态

每个 instrument 都有独立的模拟撮合引擎，维护：

- 最新外部订单簿；
- 最新成交价；
- 策略已经提交但尚未结束的订单；
- 每笔订单的已成交量和剩余量；
- 可选的盘口流动性消耗；
- 可选的队列位置；
- 报单、改单和撤单的在途指令。

策略订单和历史订单簿相互独立。策略订单不会改变后续历史行情，因此这是一种“策略订单
在历史市场上的反事实撮合”，不包含自身订单造成的市场冲击。

## 单条市场事件的处理顺序

Nautilus 回测引擎按照 `ts_init` 排列和推进事件。每条市场数据的主要处理顺序为：

```text
推进模拟时钟
    ↓
执行该时刻之前到期的 timer
    ↓
市场数据进入模拟交易所
    ↓
更新盘口或最新成交价，并撮合已有挂单
    ↓
市场数据发送给策略
    ↓
策略可能提交新订单或撤单
    ↓
处理已经到达模拟交易所的交易指令
    ↓
再次运行撮合
```

没有订单延时时，策略可能看到一份盘口后，在同一 `ts_init` 提交订单并与该盘口成交。
配置订单延时后，指令要等到未来的模拟时间才能进入交易所，并使用届时的最新盘口。

## 限价订单撮合

假设当前盘口为：

```text
买一 10.00 × 500
卖一 10.02 × 300
卖二 10.03 × 700
```

### 主动买单

买入限价满足以下条件时成为 `TAKER`：

```text
buy_limit >= best_ask
```

例如提交 `BUY 800 @ 10.03`，订单从卖一开始逐档撮合：

```text
300 @ 10.02
500 @ 10.03
```

成交使用实际盘口档位价格，不统一使用委托价。买单不会使用高于委托价的卖盘。

如果提交 `BUY 1200 @ 10.03`，上述盘口最多提供 1000 股，结果为：

```text
300 @ 10.02
700 @ 10.03
剩余 200 股继续挂在 10.03
```

### 主动卖单

卖出限价满足以下条件时成为 `TAKER`：

```text
sell_limit <= best_bid
```

订单按照买盘价格从高到低逐档成交，但不会使用低于委托价的买盘。

### 被动挂单

如果当前卖一为 10.02，提交 `BUY 400 @ 10.00`，订单不会立即成交，而是作为
`MAKER` 保留。后续每条盘口或成交 tick 都会重新驱动撮合。

被动订单可以通过两种方式成交：

1. 后续盘口穿过订单价格。例如卖一降至 10.00，买单可以按可用数量成交。
2. 后续 `TradeTick` 命中订单价格。例如 10.00 出现主动卖出成交，可以消耗挂在
   10.00 的模拟买单。

对于原本的被动买单，如果市场卖价从 10.01 直接移动到 9.99，当前本地 Nautilus
实现会保守地按订单限价记成交，而不是给予更好的 9.99 成交价。被动卖单同理。

## TradeTick 驱动的成交

`TradeTick.aggressor_side` 表示成交的主动方：

- `SELLER`：主动卖方打到买盘，可以成交模拟被动买单；
- `BUYER`：主动买方吃掉卖盘，可以成交模拟被动卖单；
- `NO_AGGRESSOR`：方向未知。

方向未知时，Nautilus 会临时将 bid 和 ask 都移动到成交价，可能使两侧订单都有机会
成交，因此结果可能偏乐观。原始数据没有主动方向时，可以使用以下规则推断：

```text
成交价 >= 当时卖一 → BUYER
成交价 <= 当时买一 → SELLER
价差内部          → 使用 tick rule
```

无法可靠推断时才使用 `NO_AGGRESSOR`。

只有 `OrderBookDepth10` 而没有 `TradeTick` 时，主动穿价订单仍可按盘口撮合，被动订单
主要依赖未来盘口穿过其价格。要更完整地模拟被动挂单，需要真实成交价、成交量和主动方向。

## 流动性消耗

`liquidity_consumption=False` 时，同一份盘口数量可能被多笔模拟订单重复使用，结果通常
偏乐观。

启用：

```python
liquidity_consumption=True
```

后，一个档位被模拟订单消耗的数量不会被其他订单重复使用。新的完整盘口快照到达时，
该档流动性重新以新快照为准。

完整 `OrderBookDepth10` 应设置：

```python
flags=RecordFlag.F_SNAPSHOT
```

这样撮合引擎可以正确重置前一快照的流动性消耗状态。

## 队列位置

开启 `queue_position=True` 后，限价订单记录提交时同价位上已经存在的数量。例如买一
10.00 有 500 股，我们随后提交 `BUY 200 @ 10.00`：

```text
第一笔主动卖出 200 → 前方剩余 300
第二笔主动卖出 200 → 前方剩余 100
第三笔主动卖出 150 → 前方 100 清空，我们成交 50
```

这只是基于可见数量的队列估算，不是真实订单 ID 级别的排队。

当前本地 Nautilus 对每份完整 `OrderBookDepth10` 快照会清理现有队列位置。频繁回放
完整五档快照时，队列可能过早归零。因此建议：

- 只有完整五档快照：先使用 `queue_position=False`；
- 有 `OrderBookDeltas` 增量盘口：可以评估使用 `queue_position=True`；
- 有逐笔委托数据：才能进一步提高排队模拟精度。

## 默认 FillModel

默认 `FillModel` 主要参数为：

```text
prob_fill_on_limit = 1.0
prob_slippage = 0.0
```

含义为：

- 成交条件满足时不额外随机拒绝成交；
- 不随机增加一个 tick 的滑点；
- 位于价差内部但没有触及对手盘的订单不会凭空成交。

只需要按照历史 tick 正常撮合时，不需要额外引入随机成交概率。

## 行情延时

`LatencyModel` 不负责行情延时。`OrderBookDepth10` 使用两个时间戳表达行情产生和到达：

```text
ts_event：交易所产生盘口的时间
ts_init ：系统收到并处理盘口的时间

market_data_latency = ts_init - ts_event
```

例如交易所于 09:30:00.000 产生盘口，系统 30ms 后收到：

```python
depth = OrderBookDepth10(
    instrument_id=instrument_id,
    bids=bids,
    asks=asks,
    bid_counts=bid_counts,
    ask_counts=ask_counts,
    flags=RecordFlag.F_SNAPSHOT,
    sequence=sequence,
    ts_event=exchange_ts_ns,
    ts_init=exchange_ts_ns + 30_000_000,
)
```

Nautilus 在 09:30:00.030 更新撮合盘口并将其通知策略。

### 行情时间字段的选择

如果数据同时包含交易所时间和本地接收时间，直接使用：

```text
ts_event = 交易所时间
ts_init  = 本地接收时间
```

如果只有交易所时间，可以采用固定延时，或者根据实盘统计使用带固定随机种子的延时
分布。如果只有采集时间，建议第一版使用：

```text
ts_event = capture_time
ts_init  = capture_time
```

不要在没有依据时虚构精确行情延时。

### 快照间隔不等于行情延时

每秒一份五档盘口表示采样频率为 1 秒，不表示行情网络延时为 1 秒。两个快照之间的真实
盘口变化未知，回测只能将上一份盘口维持到下一份快照到达。不要在快照之间人为插值盘口。

1 秒快照可以用于当前 10 秒趋势和 2 秒 convergence 检查，但不能准确模拟毫秒级抢单、
排队和撤单竞争。

### 顺序与迟到快照

Nautilus 按 `ts_init` 排序数据。人工加入不同延时后，较新的 sequence 可能先到，旧快照
随后到达并覆盖新盘口。完整快照回放应当：

- 使用真实接收时间时，丢弃晚到的旧 sequence；或者
- 人工模拟延时时，保证每个 instrument 的 `ts_init` 单调递增。

## 订单延时

订单延时由 `LatencyModel` 负责：

```python
latency_model = LatencyModel(
    base_latency_nanos=5_000_000,
    insert_latency_nanos=15_000_000,
    update_latency_nanos=15_000_000,
    cancel_latency_nanos=25_000_000,
)
```

实际延时为：

```text
报单延时 = base_latency + insert_latency
改单延时 = base_latency + update_latency
撤单延时 = base_latency + cancel_latency
```

假设盘口在 09:30:00.030 到达策略，策略立即提交订单，报单延时为 20ms，那么订单在
09:30:00.050 才进入模拟交易所，并使用该时刻撮合引擎掌握的最新盘口，而不是策略最初
看到的盘口。

具体延时应当从实盘日志统计，不建议任意设置。

## 撤单与成交竞争

撤单指令发出后，在撤单延时结束前，原订单仍处于交易所订单簿中。例如：

```text
10:00.000 策略发出撤单
10:00.050 撤单到达模拟交易所
```

如果这 50ms 内出现可成交 tick，可能发生：

```text
成交先到 → 原订单成交，随后撤单无法再取消它
撤单先到 → 原订单取消，后续 tick 不能成交该订单
```

配置订单延时并正确排列 tick 后，Nautilus 根据事件先后自然模拟这种 cancel/fill race。

## 当前回测模型的时间边界

对同一份 `OrderBookDepth10`，默认回测引擎在 `ts_init` 时同时：

1. 更新模拟交易所盘口；
2. 撮合已有订单；
3. 将盘口通知策略。

它不会在 `ts_event` 先更新交易所，再到 `ts_init` 才将行情通知策略。因此它没有完全拆开
“交易所真实市场时间轴”和“策略接收时间轴”。

对于当前秒级采样、10 秒趋势和 2 秒 convergence 周期，这通常足够。如果需要精确模拟
毫秒级行情优势，需要将行情拆成两条路径：

```text
ts_event：只更新模拟交易所
ts_init ：延迟通知策略
```

这需要定制数据路由或扩展模拟交易所，不能只依靠一条 `OrderBookDepth10` 设置两个时间戳。

## 与当前策略的执行流程

当前策略使用普通限价单，tick 回放下的完整流程为：

```text
Depth10 更新五档
    ↓
策略根据开盘价、趋势和盘口计算限价
    ↓
订单经过 insert latency 到达模拟交易所
    ↓
订单是否跨过对手盘？
    ├─ 是：按照可成交档位和数量立即全部或部分成交
    └─ 否：成为被动挂单
             ↓
        后续 Depth10 / TradeTick 持续回放
             ↓
        盘口或成交是否触及订单？
             ├─ 是：全部或部分成交
             └─ 否：继续挂单
                     ↓
                2 秒 convergence 检查
                     ↓
                超过动态 timeout？
                     ├─ 否：继续等待
                     └─ 是：发送撤单
                              ↓
                         撤单在途期间仍可能成交
                              ↓
                         撤单成功后按最新盘口重新报价
```

## 推荐的第一版配置

对于“按历史 tick 正常回放，不刻意制造场景”的第一版实现，建议：

```python
engine.add_venue(
    ...,
    book_type=BookType.L2_MBP,
    bar_execution=False,
    trade_execution=True,
    liquidity_consumption=True,
    queue_position=False,
    latency_model=LatencyModel(...),
)
```

数据按照真实时间加入：

```text
OrderBookDepth10：五档盘口
TradeTick：逐笔成交
full-tick 事件：最新价和累计成交量趋势
```

不要同时启用 Bar 撮合，否则 Bar 的 OHLC 也会移动模拟市场，与 tick 撮合重复。

## 当前 qapp 回测路径的差异

当前主回测尚未执行上述 tick 撮合：

- `bar_execution=True`；
- 主要加入 daily planning event、heartbeat quote 和执行 Bar；
- `unfilled_timeout_secs=0`，关闭未成交超时撤单；
- 回测策略未订阅 trade tick 和 order-book depth。

因此需要增加独立的 tick 数据加载与转换路径，关闭 Bar 撮合，恢复策略配置中的未成交
超时，并回放 `OrderBookDepth10`、`TradeTick` 和趋势事件，才能完整验证趋势加速撤单和
重新报价逻辑。

