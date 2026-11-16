# BigQMT 单边盘口与行情交付修复方案

状态：2026-11-13 已完成源码修改和针对性验证，未部署、未重启交易程序。

修改位于现有分支：qapp 为 auction，nautilus_trader 与 xtquant_big_convert 均为 quant_v5；没有切换分支。

传输范围：当前使用的 Redis、Pipe 及相关回调路径；按用户要求，ZMQ 不在本次修改和验证范围内。

## 目标

1. 涨停只有买盘、跌停只有卖盘时，最新 depth 正常交付并替换旧双边缓存。
2. 某股票、某数据类型的解析/交付失败不影响同批后面的正常股票，也不阻止同股票另一种数据类型的处理。
3. xtquant_big_convert 的最新行情缓冲按 topic、股票合并，不因另一只股票或空帧覆盖丢失未发送盘口。
4. 修复桥接自身可避免的发布失败、坏帧中断与静默异常；保留有限内存和非阻塞 Pipe 接收。

本方案使用 Nautilus 的现有数据接口，不增加策略直接调用 QMT 接口的行为。行情接收修复不改变涨跌停交易规则、盘口年龄阈值、收敛周期、资金门控或 risk manager。

## 一、Nautilus BigQMT depth 解析

文件：nautilus_trader/adapters/bigqmt/common.py。

- 买侧仅以 bidPrice/bidVol 判断并解析；卖侧仅以 askPrice/askVol 判断并解析。每侧最多 10 档，分别截断正常尾部零档，不能因为另一侧为空就丢弃有效侧。
- 价格必须有限且大于零，数量按现有手数单位解析，排除非有限、负数和无法转换的数据。区分空侧/零尾档和格式错误，格式错误提供 reason。
- 单边有效时仍创建 OrderBookDepth10，较短一侧用 Nautilus 的 NULL_ORDER 补齐，counts 对齐；不能复制另一侧价格、用旧一侧补齐或虚构数量。
- 两侧均无有效价量时保持返回 None，增加明确的跳过原因；不将缺字段的坏 payload 当作有效空快照清理缓存。
- QuoteTick 仍依其双边价格要求解析；单边 depth 不依赖 QuoteTick 成功。
- 不修改 Nautilus Cython/Rust 数据模型；本地运行时已验证 NULL_ORDER 可用于 OrderBookDepth10，并且双边→仅买盘→仅卖盘的 apply_depth 会清掉不存在的一侧。

这里修复的是单边数据接收，策略是否允许某方向下单仍由已有有效性和定价规则决定。

## 二、Nautilus BigQMT 批次异常隔离

文件：nautilus_trader/adapters/bigqmt/data.py。

- 先检查每股票 payload 形状，再映射 instrument_id。
- quote 和 depth 各自隔离解析与 _handle_data 交付异常；quote 失败仍尝试 depth，depth 失败也不影响后面的股票。
- 日志包含 code、数据类型、阶段（parse/deliver）、异常类型/原因、计数，不输出完整 tick/order payload。高频重复异常限频。
- 成功交付的数据仍按现有 tick_trace 路径记录；诊断失败不能中断正常批次处理。
- 隔离普通行情处理 Exception；不吞进程终止/任务取消，也不改变数据引擎自己的 shutdown-on-error 策略。

## 三、xtquant_big_convert 行情缓冲

主要文件：src/bigqmt_signal_trader/redis_pipe_proxy.py。

合并规则：

```text
topic T pending = {A: A1}
收到 {B: B1}      => {A: A1, B: B1}
收到 {A: A2}      => {A: A2, B: B1}
收到 {}          => 保持原 pending
```

- 按股票替换整份 tick，而不是按字段叠加；新 tick 的空卖盘必须保留，不能继承旧 tick 的卖盘。每股票 _trace 随所选 tick 一起替换。
- 收包线程只做校验和内存入队，不做 Redis I/O、重试等待或阻塞式 put。
- 就绪 topic 队列满时，不再删除最老 topic 的 pending；新 topic 保留在待调度集合，worker 按公平顺序补入就绪队列。每 topic/股票仅保留最新 tick，内存随活跃订阅股票数增长，不随积压帧数增长；确认最终引用退订后清理待发送状态，近期退订 topic 的标记 30 秒后过期。
- Redis publish 失败：把失败批次合回 pending；已有的新 tick 优先，失败旧 tick 仅补回不存在的股票。退避仅发生于后台发布线程，并可被 stop 唤醒。
- 非法股票条目/无法编码的股票隔离并记录 reason，避免一个坏条目永久阻塞整个 topic 的重试。
- 健康停机给予行情有限时间排空；超时退出时记录未发送 topic/股票数量。
- 更新 quote_queue_capacity 的说明：它约束就绪调度队列，不再作为删除 pending 行情的依据。

## 四、xtquant_big_convert 接收和回调边界

文件：quote_push_channel.py、transports/pipe_push_channel.py、whole_quote_session.py、quote_subscription_manager.py，必要时 xtquant_compat.py 的 primer 路径。

- Redis/Pipe 的坏 JSON、错误外层形状、错误股票条目隔离到当前帧/股票；后续正常帧继续处理。
- Redis 的解码异常不能被当作连接故障而关闭健康订阅；真实连接异常走现有重连。
- Pipe 合法 JSON 非 dict 不再让整个接收线程静默退出。真实 I/O 故障记录原因，并遵守原有线程持有/关闭连接的约束。
- session callback、primer callback 的异常由静默吞掉改为限频报告；一个 callback 失败仍继续其他 callback。
- 源 publisher 异常限频报告，不抛回 QMT 同步首回调；保留已建立的源订阅，后续正常推送继续处理。
- 源订阅创建期间预先登记 topic，订阅失败回滚，解决同步首回调早于 combo 注册的静默丢弃窗口；测试订阅失败和退订在途回调，防止幽灵订阅。
- 计数至少区分：同股票旧快照合并、非法帧、非法股票、解析失败、交付失败、发布失败/恢复、停机未发送。正常单边盘口不计为错误。

Redis Pub/Sub 与 Pipe 断连期间没有历史重放；本方案不能承诺补回源端未推送或断连期间未收到的每一笔 tick。先修复本代码里的可避免丢失，保持“每股票最新盘口”的语义，不引入新的持久化消息系统。

## 验证与实施顺序

1. Nautilus parser：双边、仅买侧/仅卖侧（数组缺失、空数组、全零）、侧间长度不同、零尾档、非法数值、正常 10 档。
2. 真实 OrderBook 与 qapp 缓存：双边→单边，验证旧一侧清空，ts_event/ts_init/observed_ns 更新，现有交易规则继续生效。
3. 批次：A 坏 B 正常、A quote 失败但 depth 正常、A 交付失败 B 正常、诊断失败 B 正常。
4. relay：不同股票增量合并、同股票替换和 trace、空帧、队列满、多 topic 公平发送、并发收包、publish 失败时新旧 tick 合并、stop 超时计数。
5. 接收边界：坏帧后正常帧仍交付、连接错误保持可恢复、回调异常不阻断其他回调、同步首回调可正确路由。
6. 使用现有真实构造路径和 I/O mocks；运行 BigQMT parser/data 和桥接相关测试，再运行 qapp 受影响的盘口/定价测试。Linux mocks 验证逻辑不等同于已完成 Windows QMT 实机端到端验证。

## 已完成的验证

- Nautilus BigQMT parser/data：78 项通过；修改文件的 Ruff 检查通过。
- 桥接核心及源回调：96 项通过；周边订阅、重连、RPC、Pipe 和多账号用例另一次运行 58 项通过、5 项因 Windows 环境条件跳过（包含与核心运行重叠的 6 项源回调用例）。
- qapp 定价和执行器：146 项通过，排除 2 项既有失败；新单边盘口缓存用例通过。两项失败使用 HEAD 中修改前的测试文件复现：test_apply_full_tick_logs_complete_first_sorted_record、test_live_sizing_prefers_broker_total_asset_over_cash_only_equity。对应 qapp 业务实现本次未修改。
- Pipe 单文件 src/BIGQMT_DRYRUN_PIPE_FLAT_ALL_IN_ONE.py 已重新生成；编译通过，新增 quote_diagnostics 模块已内嵌，所有顶层 BIGQMT 配置赋值与原产物一致。
- ZmqQuotePushChannel 的 AST 与修改前相同；未修改 ZMQ 实现。
- 三个仓库 git diff --check 均通过。
- 按 Nautilus AGENTS.md 尝试 make security-audit，环境缺少 cargo-audit，命令在前置检查阶段退出；没有安装额外审计工具。

本地性能对照：500 只股票、每只同时解析 quote 和三档 depth、关闭 tick_trace、交付到测试 collector，9 次交替运行的批次耗时中位数从 14.641 ms 变为 16.104 ms。正常发布批次仅序列化一次；仅编码失败后才逐股票检查。该对照不包含实盘策略、Windows Pipe 或 Redis 端到端耗时。数据保存在 output/analysis/book_delivery_fix_20261113/batch_processing_benchmark.json。

修改均保留在工作区，未提交 Git commit；没有部署或重启交易程序。Windows QMT 实机端到端验证尚未进行。
