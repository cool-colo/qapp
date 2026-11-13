# BigQMT tick 全链路日志

默认关闭，以下两种方式均可开启 adapter 诊断事件和 qapp 日志 Actor：

```bash
# 加到现有 BigQMT 启动命令上
python lives/live_bigqmt_target_model_predictions.py --config configs/strategy.yaml \
  --account-id YOUR_ACCOUNT --bigqmt-tick-trace

# 或在现有部署环境/.env 中设置
export BIGQMT_TICK_TRACE_ENABLED=1
```

命令行覆盖环境变量：`--no-bigqmt-tick-trace` 可在环境变量为 `1` 时关闭。
环境变量支持 `1/0`、`true/false`、`yes/no`、`on/off`，不区分大小写；非法值报错。
诊断需要现有 tick 推送链路，使用 `BIGQMT_USE_QUOTE_PUSH=1` 开启该链路。
诊断开关自身不切换行情来源或修改轮询设置。

开启时，启动入口把 `tick_trace_enabled=True` 传入 `BigQMTDataClientConfig`，
并注册独立的 `BigQMTTickTraceLogger` Actor。Actor 订阅 `bigqmt.tick_trace.*`，
回调只捕获 `qapp_received_ns` 并非阻塞入队，后台线程序列化和提交 Nautilus INFO 日志。
日志沿用现有控制台和每日文件配置，不新增数据库写入：

```text
live_tick_trace {"instrument_id":"000001.SZ.BIGQMT","data_kind":"depth10","ts_event":...,"ts_init":...,"qmt_callback_ns":...,"pipe_publish_ns":...,"proxy_pipe_received_ns":...,"redis_publish_ns":...,"adapter_redis_received_ns":...,"adapter_callback_ns":...,"qapp_received_ns":...,"dropped_events_total":0}
```

每行保留原始 `ts_event`、`ts_init` 和全部阶段时间，单位 UNIX 纳秒；缺失阶段为 JSON `null`。
`data_kind` 为 `quote` 或 `depth10`，同时订阅两种行情会记录两条事件。
`qapp_received_ns` 是诊断 Actor 收到事件的时间；诊断事件在标准行情提交后发布，
该字段不能直接当作策略处理该行情的入口时间。

关闭时不注册 Actor、不起日志线程，adapter 不构建或发布诊断事件。
桥接侧原始 tick 仍携带时间戳，所以关闭事件开关不代表桥接打点完全没有开销。

队列固定容纳 8192 条事件。队列满时只丢弃本次诊断日志，不阻塞或改变行情处理，
日志中的 `dropped_events_total` 显示累计丢弃数；停止时汇总丢弃和日志失败计数。
停止/销毁 Actor 会先退订，再让后台线程排空，最多等待 1 秒。
INFO 日志必须启用才能看到这些记录。全量日志会增加后台 CPU 和磁盘开销，
排查结束后可关闭开关。

使用同组完整样本计算各相邻阶段差值和 `ts_init - ts_event` 的均值/P95。
各段 P95 不可相加；跨主机需同步时钟，缺失字段和负差值单独标记。
桥接字段含义及局部性能测量见配套桥接仓库 `docs/tick_trace.md`。
