# 价格、成交量与五档盘口趋势驱动的订单执行

## 目标

在不改变目标仓位决策的前提下，根据短期价格走势、成交量和五档盘口压力调整订单执行：

- BUY 遇到价格快速上涨时，提高买入紧迫度；
- SELL 遇到价格快速下跌时，提高卖出紧迫度；
- 顺向于订单的有利走势不提高紧迫度；
- 趋势只影响未成交订单等待时间和首次报价偏移，不改变 risk manager 给出的目标数量。

实现位于 `strategies/target_quantities.py`，报价调整位于
`strategies/pricing/open_offset.py`。

## 信号计算

### 1. 价格收益率

使用普通策略 full-tick 中的 `last_price` 建立每只相关股票的滚动窗口，不使用全市场
快照：

```text
price_return_bps = (last_price / first_price - 1) * 10000
```

默认窗口为 10 秒。bps 与股票绝对价格无关：1% 始终等于 100 bps。

### 2. 成交量确认

full-tick 的 `volume` 是累计成交量。对相邻样本计算增量成交量，并判断该区间的价格
方向是否与窗口总体价格方向一致：

```text
volume_confirmation =
    Σ(delta_volume * interval_direction * price_direction) / Σ(delta_volume)
```

结果范围为 `[-1, 1]`：

- `1`：增量成交量全部支持当前价格方向；
- `0`：没有明显方向；
- `-1`：增量成交量主要反对当前价格方向。

累计成交量回退、缺失或没有正增量时，本项不参与确认，不做兼容性猜测。

### 3. 五档盘口 OBI

仅使用最新、双边、非交叉且未超过两秒的 Nautilus order-book depth。最多读取五档，
档位权重固定为：

```text
w_i = 1 / i,  i = 1..5
```

```text
bid_pressure = Σ(w_i * BidSize_i)
ask_pressure = Σ(w_i * AskSize_i)

OBI = (bid_pressure - ask_pressure) / (bid_pressure + ask_pressure)
```

结果范围为 `[-1, 1]`：正数表示买盘压力较强，负数表示卖盘压力较强。越靠近最优价
的档位权重越高。价格只用于识别交叉盘口，不直接进入 OBI 公式。

### 4. 综合确认

OBI 先按照价格方向对齐：

```text
aligned_obi = sign(price_return_bps) * OBI
```

有效的成交量确认与 OBI 等权平均：

```text
confirmation_score = average(available confirmations)
```

只有一项有效时直接使用该项；两项都无效时为 `0`。它是无量纲的一致程度，不是概率。

```text
confirmation_factor = 1 + confirmation_strength * confirmation_score
trend_bps = price_return_bps * confirmation_factor
```

默认 `confirmation_strength=0.5`，所以确认因子限制在 `[0.5, 1.5]`。成交量和盘口
只能加强或削弱价格趋势，不能改变价格方向，也不能在没有有效价格趋势时独立触发动作。

## 对订单执行的影响

只有不利走势计算执行严重程度：

- BUY：`trend_bps > 0`；
- SELL：`trend_bps < 0`。

默认在 20 bps 以内不调整，20–40 bps 之间线性提高，40 bps 及以上封顶：

```text
severity = clamp((abs(trend_bps) - 20) / (40 - 20), 0, 1)
```

应用结果：

```text
timeout_multiplier = 1 - severity * (1 - 0.3)
offset_multiplier = 1 + severity * (3 - 1)
```

- 剧烈不利趋势下，未成交等待时间最多缩短到基础值的 `0.3` 倍；
- 首次下单的 `buy_offset_bps` / `sell_offset_bps` 最多放大到 `3` 倍；
- BUY 最大价格、涨跌停限制、部分成交合法性、symbol freeze 等既有保护继续优先；
- 订单发生首次撤单后，继续使用既有的累计深度覆盖与逐 tick 穿透定价，趋势乘数不再
  改变该阶段的 depth-walk 行为；
- 达到有效超时并不会主动唤醒撤单；撤单在下一次成功执行的 convergence cycle 中检查。
  当前周期配置为 2 秒，因此可能在超过有效超时后继续等待最多约一个检查周期；其它
  convergence 触发事件也可能更早执行检查。

## 配置

配置位于 `configs/strategy*.yaml`，并由 `StrategyParams` 校验：

```yaml
price_trend_window_secs: 10.0
price_trend_threshold_bps: 20.0
price_trend_max_bps: 40.0
price_trend_confirmation_strength: 0.5
price_trend_book_max_age_secs: 2.0
price_trend_min_timeout_multiplier: 0.3
price_trend_max_offset_multiplier: 3.0
```

非法窗口、阈值顺序、确认强度或乘数会在启动时直接抛出 `ValueError`。

## 可观测性与持久化

下单时，下列字段进入 `TargetQuantityOrderEvent.extra.intent_context.price_context`：

```text
price_return_bps
volume_confirmation
book_imbalance
confirmation_score
trend_bps
trend_severity
timeout_multiplier
offset_multiplier
```

因未成交超时取消时，取消时刻的最新值还会写入 `intent_context` 顶层，并额外记录
`effective_timeout_secs`。

- 回测写结果时，这些内容进入 MySQL `order.extra` JSON；
- 实盘当前仅保存在 strategy 的内存事件和日志里；`SnapshotRecorder` 写入的
  `live_order` 尚未持久化这组 `intent_context` 字段；
- `live_order.book_snapshot` 是盘口快照，与这里的趋势计算结果不是同一字段。

## 边界行为

- 不足完整窗口：趋势保持中性；
- 无成交量：只使用有效 OBI；
- 无有效盘口：只使用成交量确认；
- 两者都缺失：直接使用原始价格收益率；
- OBI 存在但没有有效价格窗口：OBI 不独立触发执行调整；
- 交易日切换：价格历史、盘口时间戳和日内状态全部重置；
- 回测路径设置 `unfilled_timeout_secs=0`，因此不会启用趋势驱动的撤单重报。
