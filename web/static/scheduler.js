/* Read-only scheduler presentation. No trading commands or condition evaluation. */
(function (root) {
  "use strict";
  const TASKS = {
    bootstrap: "启动初始化", daily_planning: "每日初始规划", auction_planning: "竞价目标规划",
    auction_execution: "集合竞价执行", normal_execution: "正常交易执行", scheduled_replan: "定时重规划",
    price_limit_replan: "涨跌停重规划", exposure_correction: "总仓位纠偏",
    manual_replan: "手动重规划", manual_execution: "手动交易执行",
  };
  const STATES = {
    queued: "等待执行", preparing: "启动前清理中", running: "执行中", waiting: "等待条件", suspending: "抢占 / 暂停清理中",
    suspended: "已挂起", stopping: "结束清理中", succeeded: "已成功", failed: "已失败",
    unachievable: "无法达成", cancelled: "已取消", expired: "已过期",
  };
  const ACTIONS = {
    QUEUED: "任务入队", COALESCED: "合并触发", ADMITTED: "获准执行", PROGRESS: "执行进展",
    OUTCOME_DECIDED: "任务结果已确定", CLEANUP_REQUESTED: "框架请求清理", CLEANUP_REDIRECTED: "调整清理流程", STABLE_STATE_REQUESTED: "业务请求干净状态", RESUMED_CLEAN: "清理后继续业务", WAITING_CLEANUP: "等待清理",
    CLEANUP_COMPLETED: "清理完成", CLEANUP_SKIPPED: "按任务配置跳过清理", TERMINATED: "任务终结", SUSPENDED: "任务挂起",
    COMPONENT_ERROR: "组件异常", TRIGGER_ERROR: "触发评估异常", AUDIT_ERROR: "记录异常",
  };
  const REASONS = {
    trigger: "触发条件满足", new_trigger: "收到新触发，合并到已有任务", priority_and_scope: "按优先级获得执行权",
    priority_and_stable_scope: "按优先级获得执行权，状态已稳定",
    priority_and_cleanup_policy: "按优先级及任务清理配置获得执行权",
    cleanup_skipped_by_task_policy: "按任务配置跳过本次清理，未确认状态稳定",
    forced_cleanup_after_skip: "跳过清理后收到中断请求，转入强制清理",
    initialized_from_business_facts: "已根据当前交易状态完成初始化", formal_target_committed: "正式目标已提交",
    reuse_current_target: "沿用当前正式目标", target_achieved: "当前执行目标已达成", target_remaining: "仍有目标数量待执行",
    proposal_expired: "触发请求已超过有效期限", auction_submit_window_closed: "竞价报单窗口已结束",
    normal_window_closed: "普通执行窗口已结束", auction_orders_tracked: "正在跟踪竞价报单",
    execution_stable: "订单与执行状态已稳定", cancelling_existing_orders: "等待已有订单撤单确认",
    cancellation_window_closed: "等待撤单窗口开放", cancellation_requires_reconciliation: "撤单未确认，等待对账",
    awaiting_cancellation_batch: "等待整批撤单结束或超时", awaiting_terminal_orders: "等待订单终结", retrying_cancellations: "重试被拒撤单",
    reconciliation_observation_wait: "订单无进展，对账退避等待", cleanup_reconciliation_disabled: "清理对账已关闭，等待订单终结",
    external_orders_in_account: "账户存在其他策略的未完成订单", existing_planning_in_progress: "等待已有规划完成",
    auction_planning_market_pending: "竞价规划等待新行情",
    invalid_auction_planning_snapshot: "竞价规划行情不完整", auction_target_unavailable: "竞价缺少当日目标",
    scheduled_planning_not_applicable: "定时规划业务条件不满足",
    reconciling: "正在对账", reconciliation_failed: "对账失败，等待重试", reconciliation_retry_wait: "等待对账重试时间",
    reconciliation_completed: "对账完成，重新确认稳定状态", settling: "等待执行状态稳定",
    user_paused: "交易或目标更新已暂停", automatic_execution_paused: "自动执行已暂停",
    automatic_planning_protected: "自动规划受当前保护状态限制", within_tolerance: "总仓位处于容忍范围内",
    exposure_within_tolerance: "总仓位纠偏已完成", outside_tolerance: "总仓位超出容忍范围",
    single_side_correction: "正在执行单方向仓位纠偏", awaiting_corrective_orders: "等待纠偏订单完成",
    reassess_after_risk_manager: "正式目标已重规划，重新评估仓位", unchanged_insufficient_capacity: "可执行容量未变化，等待条件改善",
    request_risk_manager_capacity_insufficient: "剩余目标容量不足，请求正式重规划",
    missing_auction_snapshot: "等待竞价行情快照", no_current_formal_target: "尚无当日正式目标",
    cancelled: "任务已被取消", shutdown: "节点停止", definition_disabled: "任务已停用",
    preempt_or_pause: "响应抢占或暂停，先清理当前执行", stop: "任务结束，先清理当前执行",
    optimizer_pending: "等待风险管理器返回规划结果", optimizer_timeout: "风险管理器规划超时",
    no_feasible_plan: "风险管理器未返回可执行方案", planning_finished: "规划流程已完成",
    planning_window_closed: "规划窗口已结束", correction_window_closed: "仓位纠偏窗口已结束",
    planning_authorization_expired: "规划授权已失效", planning_snapshot_stale: "规划期间交易状态发生变化",
    state_changed_during_persistence: "目标归档期间交易状态发生变化", planning_interrupted: "规划已被中断",
    manual_execution_window_closed: "等待手动交易窗口开放", manual_target_evaluated: "已检查手动交易目标",
    invalid_asset_denominator: "缺少有效的资产数据，等待重新评估", invalid_position: "持仓数量数据无效",
    missing_position_price: "缺少持仓估值行情，等待重新评估",
  };
  const escape = (value) => String(value == null ? "" : value).replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const name = (id) => TASKS[id] || id;
  const reason = (value) => REASONS[value] || value || "暂无原因记录";
  const time = (value) => value ? String(value).replace("T", " ").slice(0, 23) : "—";
  function badge(state, label) {
    const tone = ["failed", "unachievable"].includes(state) ? "error" :
      state === "succeeded" ? "success" : ["preparing", "running", "waiting", "stopping", "suspending"].includes(state) ? "active" : "neutral";
    return `<span class="sched-badge ${tone}">${escape(label || STATES[state] || state || "未执行")}</span>`;
  }
  function taskName(id) {
    return `<b>${escape(name(id))}</b><small class="sched-id">${escape(id)}</small>`;
  }
  function details(id, value, label = "结果与上下文") {
    return `<details class="sched-detail" data-sched-detail="${escape(id)}"><summary>${escape(label)}</summary>` +
      `<pre>${escape(JSON.stringify(value, null, 2))}</pre></details>`;
  }
  function triggerText(task, snapshotTime) {
    const trigger = task.trigger;
    if (trigger.applicable === false) return "集合竞价功能未启用";
    if (trigger.kind === "startup") return "节点启动时执行一次";
    if (trigger.kind === "event") return "收到当日规划事件时触发";
    if (trigger.kind === "manual") return "收到手动请求后触发";
    if (trigger.kind === "time") {
      if (!trigger.times.length) return "未配置定时触发时刻";
      const clock = snapshotTime ? snapshotTime.split("T")[1].slice(0, 8) : null;
      const next = clock && [...trigger.times].sort().find(t => (t.length === 5 ? t + ":00" : t) > clock);
      return next ? `今天 ${next} 到时触发，仍需准入` : `配置时刻：${trigger.times.join("、")}`;
    }
    if (task.task_id === "price_limit_replan") return "涨跌停重规划条件满足时触发";
    const condition = task.task_id === "exposure_correction" ? "总仓位超出容忍范围" : "执行目标尚未完成";
    return `${condition} · 窗口 ${trigger.windows}`;
  }
  function outcome(result, cleanup = false) {
    if (!result) return `<span class="hint">${cleanup ? "尚未开始清理" : "尚未产生最终结果"}</span>`;
    const skipped = cleanup && result.evidence && result.evidence.cleanup_skipped === true;
    const label = skipped ? "清理已跳过" : cleanup ? ({ succeeded: "清理已完成", waiting: "等待清理确认", failed: "清理异常" }[result.state]) : null;
    return `${badge(skipped ? "neutral" : result.state, label)}<span>${escape(reason(result.reason))}</span>`;
  }
  function configurationDetails(task, configuration) {
    const periodic = task.trigger.kind === "condition";
    const interval = periodic ? `${task.interval_ns / 1e9} 秒` :
      ({ startup: "启动时触发", event: "按事件触发", time: "按配置时刻触发", manual: "按手动请求触发" }[task.trigger.kind]);
    const rows = [
      ["条件检查周期", periodic ? interval : `${interval}，无周期条件检查`],
      ["可抢占其他任务", task.can_preempt ? "可以，需满足优先级和清理条件" : "不可以"],
      ["可被其他任务抢占", task.preemptible ? "可以，先清理再交接执行权" : "不可以"],
      ["拆单组件", task.splitter || "由内置流程处理"],
      ["执行撤单组件", task.cancellation || "由内置流程处理"],
      ["框架清理组件", configuration.cleanup_component],
      ["入场清理", task.skip_entry_cleanup ? "跳过（不确认状态稳定）" : "执行完整清理"],
      ["正常成功退出清理", task.skip_exit_cleanup ? "跳过（仍中断异步业务并释放临时状态）" : "执行完整清理"],
      ["强制清理", "抢占、暂停、停止、取消、失败、无法达成、过期及业务请求干净状态时始终执行"],
      ["下次条件检查", task.enabled ? time(task.next_evaluation_at) : "停用期间不检查"],
    ];
    if (task.trigger.cutoff) rows.push(["报单截止", task.trigger.cutoff]);
    if (task.task_id === "exposure_correction") {
      rows.push(["仓位容忍偏差", `${Number(configuration.exposure.tolerance) * 100} 个百分点`]);
      rows.push(["纠偏评估组件", configuration.exposure_components.assessor]);
      rows.push(["纠偏规划组件", configuration.exposure_components.planner]);
    }
    return `<details class="sched-detail" data-sched-detail="task-${escape(task.task_id)}"><summary>查看配置</summary>` +
      `<dl class="sched-config">${rows.map(([label, value]) => `<dt>${escape(label)}</dt><dd>${escape(value)}</dd>`).join("")}</dl>` +
      details(`raw-task-${task.task_id}`, task.task_id === "exposure_correction" ?
        { task, exposure: configuration.exposure, components: configuration.exposure_components } : task, "完整配置与字段") + "</details>";
  }
  function render(data, renderTable) {
    const $ = (selector) => document.querySelector(selector);
    const open = new Set([...document.querySelectorAll("details[data-sched-detail][open]")].map(e => e.dataset.schedDetail));
    const summary = data.summary;
    const status = data.paused ? "调度已暂停" : ({ ready: "调度运行中", initializing: "启动初始化中", stopping: "停止清理中", stopped: "调度已停止" }[data.lifecycle] || data.lifecycle);
    $("#sched-status").textContent = status;
    $("#sched-status").className = "sched-badge " + (data.lifecycle === "ready" && !data.paused ? "success" : "neutral");
    $("#sched-summary").innerHTML = [
      ["配置任务", summary.configured, `${summary.enabled} 项启用，含内置启动任务`],
      ["正在执行", summary.current, data.current.length ? name(data.current[0].task_id) : "当前没有任务占有执行权"],
      ["等待执行", summary.queued, "已入队或挂起，尚未获得执行权"],
      ["执行次数", summary.executed, `${summary.succeeded} 次成功 · ${summary.failed} 次失败 / 无法达成`],
    ].map(([label, count, detail]) => `<div class="sched-stat"><span>${label}</span><strong>${count}</strong><small>${escape(detail)}</small></div>`).join("");
    $("#sched-current-count").textContent = `${data.current.length} 个当前任务`;
    $("#sched-current").innerHTML = data.current.length ? data.current.map(run =>
      `<article class="sched-current-card"><div class="sched-run-head">${taskName(run.task_id)}${badge(run.state)}</div>` +
      `<p class="sched-reason">${escape(reason(run.reason))}</p>` +
      `<p class="hint">实例 ${escape(run.run_id)} · 优先级 ${run.priority} · 开始 ${time(run.started_at)}</p>` +
      `<div class="sched-result-grid"><div><span>任务结果</span>${outcome(run.final_result)}</div>` +
      `<div><span>清理状态</span>${outcome(run.cleanup_result, true)}</div></div>` +
      details(`current-${run.run_id}`, run) + "</article>").join("") :
      '<div class="sched-empty">当前没有任务正在执行<span>任务触发并获准后，会显示在这里。</span></div>';
    const execution = data.execution;
    $("#sched-execution").textContent = execution.complete == null ? "尚无执行器快照" :
      `当前执行目标：${execution.complete ? "已达成" : `${execution.remaining_instruments} 只股票数量尚未达标`} · ` +
      `未完成订单 ${execution.pending_orders} · 冻结股票 ${execution.frozen_instruments}`;
    $("#sched-queue-count").textContent = `${data.queue.length} 个已入队候选`;
    $("#sched-queue").innerHTML = data.queue.length ?
      `<p class="hint">按优先级与入队顺序排列；实际执行还需满足窗口、抢占和清理条件。</p>` +
      data.queue.map((run, index) => `<div class="sched-queue-item"><span class="sched-rank">${index + 1}</span>` +
        `<div>${taskName(run.task_id)}<small>${escape(run.run_id)} · 优先级 ${run.priority} · ${escape(data.paused ? "调度暂停中" : data.current.length ? "等待执行权交接" : "等待调度准入")}</small></div>${badge(run.state)}</div>`).join("") :
      '<div class="sched-empty compact">没有已入队任务<span>尚待触发的任务不会作为确定的执行计划。</span></div>';
    $("#sched-waiting").innerHTML = data.waiting.length ? data.waiting.map(task =>
      `<div class="sched-waiting-item"><b>${escape(name(task.task_id))}</b><span>${escape(triggerText(task, data.snapshot_time))}</span>` +
      (task.next_evaluation_at ? `<small>下次条件检查：${time(task.next_evaluation_at)}，不是执行时刻</small>` : "") + "</div>").join("") : '<p class="hint">没有其他启用的任务等待触发。</p>';
    renderTable("#sched-tasks", data.tasks, {
      columns: ["task_id", "enabled", "priority", "trigger", "executed_count", "last_state", "details"],
      headers: { task_id: "任务", enabled: "开关", priority: "优先级", trigger: "触发与窗口", executed_count: "执行次数", last_state: "最近状态", details: "配置详情" },
      intCols: new Set(["priority", "executed_count"]),
      cellFn: (column, value, row) => {
        if (column === "task_id") return `<td class="text sched-task-cell">${taskName(value)}${row.builtin ? '<small>内置任务</small>' : ""}</td>`;
        if (column === "enabled") return `<td><span class="sched-badge ${value ? "success" : "neutral"}">${value ? "启用" : "停用"}</span></td>`;
        if (column === "trigger") return `<td class="text sched-trigger-cell">${escape(triggerText(row, data.snapshot_time))}</td>`;
        if (column === "last_state") return `<td>${badge(value)}</td>`;
        if (column === "details") return `<td>${configurationDetails(row, data.configuration)}</td>`;
      },
    });
    const taskSelect = $("#sched-history-task"), selected = taskSelect.value;
    taskSelect.innerHTML = '<option value="">全部任务</option>' + data.tasks.map(t => `<option value="${escape(t.task_id)}">${escape(name(t.task_id))}</option>`).join("");
    taskSelect.value = selected;
    if ($("#sched-history-state").options.length === 1) $("#sched-history-state").innerHTML +=
      Object.entries(STATES).map(([key, label]) => `<option value="${key}">${label}</option>`).join("");
    renderHistory(data, renderTable);
    renderTable("#sched-actions", data.actions, {
      columns: ["sequence", "time", "task_id", "run_id", "action", "reason", "details"],
      headers: { sequence: "动作序号", time: "时间", task_id: "任务", run_id: "实例", action: "调度动作", reason: "原因", details: "上下文" },
      intCols: new Set(["sequence"]),
      cellFn: (column, value, row) => {
        if (column === "time") return `<td class="text sched-time">${time(value)}</td>`;
        if (column === "task_id") return `<td class="text">${escape(name(value))}</td>`;
        if (column === "action") return `<td class="text">${escape(ACTIONS[value] || value)}</td>`;
        if (column === "reason") return `<td class="text sched-trigger-cell">${escape(reason(value))}</td>`;
        if (column === "details") return `<td>${details(`action-${row.sequence}`, row, "查看上下文")}</td>`;
      },
    });
    document.querySelectorAll("details[data-sched-detail]").forEach(e => { if (open.has(e.dataset.schedDetail)) e.open = true; });
  }
  function renderHistory(data, renderTable) {
    const task = document.querySelector("#sched-history-task").value;
    const state = document.querySelector("#sched-history-state").value;
    const rows = data.history.filter(r => (!task || r.task_id === task) && (!state || r.state === state));
    document.querySelector("#sched-history-note").textContent =
      `本次节点运行共执行 ${data.history_total} 次；展示最近 ${data.history.length} 次中的 ${rows.length} 条。时间未知显示 —，未获准的排队任务不计入执行次数。`;
    if (!rows.length) { document.querySelector("#sched-history").innerHTML = '<div class="sched-empty compact">暂无符合条件的执行记录</div>'; return; }
    renderTable("#sched-history", rows, {
      columns: ["task_id", "run_id", "state", "started_at", "finished_at", "reason", "details"],
      headers: { task_id: "任务", run_id: "实例", state: "执行状态", started_at: "开始时间", finished_at: "结束时间", reason: "当前 / 最终原因", details: "结果与清理" },
      cellFn: (column, value, row) => {
        if (column === "task_id") return `<td class="text">${taskName(value)}</td>`;
        if (column === "state") return `<td>${badge(value)}</td>`;
        if (column.endsWith("_at")) return `<td class="text sched-time">${time(value)}</td>`;
        if (column === "reason") return `<td class="text sched-trigger-cell">${escape(reason(value))}</td>`;
        if (column === "details") return `<td>${details(`history-${row.run_id}`, { final_result: row.final_result, cleanup_result: row.cleanup_result, reason: row.reason }, "查看结果")}</td>`;
      },
    });
  }
  const ui = { render, renderHistory, triggerText, outcome, configurationDetails, name, reason, escape, time };
  root.SchedulerUI = ui;
  if (typeof module !== "undefined" && module.exports) module.exports = ui;
})(globalThis);
