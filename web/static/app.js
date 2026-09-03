"use strict";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.remove("show"), 4000);
}

async function api(path, params, opts = {}) {
  const url = new URL(path, window.location.origin);
  Object.entries(params || {}).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, v);
  });
  const resp = await fetch(url, opts);
  if (!resp.ok) {
    let detail = resp.statusText;
    try { detail = (await resp.json()).detail || detail; } catch (e) {}
    throw new Error(`${resp.status}: ${detail}`);
  }
  return resp.json();
}

function fmt(v) {
  if (v === null || v === undefined || v === "") return "";
  if (typeof v === "number") {
    if (Number.isInteger(v)) return v.toLocaleString();
    return v.toLocaleString(undefined, { maximumFractionDigits: 4 });
  }
  return String(v);
}
function pct(v) {
  if (v === null || v === undefined || v === "") return "";
  return (v * 100).toFixed(2) + "%";
}
function todayISO() { return new Date().toISOString().slice(0, 10); }

const NUMERIC_RE = /_rate|_bps|price|value|asset|cash|pnl|amount|qty|volume|weight|score|return|equity|commission|balance/;
function isNumericCol(col) { return NUMERIC_RE.test(col); }

const RATE_COLS = new Set([
  "strat_daily_rate", "csi1000_daily_rate", "excess_daily_rate",
  "week_cum_strat_rate", "week_cum_csi1000_rate", "week_cum_excess_rate", "target_weight",
]);

// A-share color convention: red = positive, green = negative, neutral = zero.
function signClass(v) {
  if (typeof v !== "number") return "";
  if (v > 0) return " pos";
  if (v < 0) return " neg";
  return " zero";
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
const state = { source: null, accounts: [], account: null };
function accountLabel(a) { return `${a.account_id} / ${a.trader_id}`; }
function accountKey(a) { return `${a.account_id}|${a.trader_id}`; }
function accountParams() {
  return { source: state.source, account: state.account.account_id, trader: state.account.trader_id };
}

// ---------------------------------------------------------------------------
// Bootstrapping
// ---------------------------------------------------------------------------
async function initSources() {
  const { sources } = await api("/api/sources");
  const sel = $("#sel-source");
  sel.innerHTML = sources.map((s) => `<option>${s}</option>`).join("");
  state.source = sources[0];
  sel.addEventListener("change", async () => { state.source = sel.value; await loadAccounts(); });
  await loadAccounts();
}

async function loadAccounts() {
  const { accounts } = await api("/api/accounts", { source: state.source });
  state.accounts = accounts;
  const sel = $("#sel-account");
  sel.innerHTML = accounts.map((a) => `<option value="${accountKey(a)}">${accountLabel(a)}</option>`).join("");
  state.account = accounts[0] || null;
  sel.onchange = () => {
    state.account = state.accounts.find((a) => accountKey(a) === sel.value);
    onAccountChanged();
  };
  onAccountChanged();
}

function onAccountChanged() {
  if (!state.account) return;
  loadSnapshotDates();
  // Apply the account's configured report window as defaults.
  $("#report-start").value = state.account.report_start || "";
  $("#report-end").value = state.account.report_end || todayISO();
  populateCompareSelectors();
  refreshActiveTab();
}

// Re-render whatever tab is currently open so switching account/source updates
// the visible page immediately instead of leaving stale content.
function refreshActiveTab() {
  const active = document.querySelector(".tab-panel.active");
  if (!active) return;
  const tab = active.id.replace(/^tab-/, "");
  if (tab === "snapshot") return; // loadSnapshotDates() already reloads it
  if (tab === "report") loadReport();
  else if (tab === "series") { if ($("#series-start").value && $("#series-end").value) $("#series-plot").click(); }
  else if (tab === "compare") { if (cmpSeries.length) plotCompare(); }
  else if (tab === "kline") { if ($("#kline-code").value.trim()) plotKline(); }
}

// ---------------------------------------------------------------------------
// Sidebar nav + snapshot sub-tabs
// ---------------------------------------------------------------------------
$$(".nav-item").forEach((btn) => {
  btn.addEventListener("click", () => {
    $$(".nav-item").forEach((b) => b.classList.remove("active"));
    $$(".tab-panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    $(`#tab-${btn.dataset.tab}`).classList.add("active");
    if (btn.dataset.tab === "report") loadReport();
    setTimeout(resizeCharts, 0);
  });
});

$$(".subtab").forEach((btn) => {
  btn.addEventListener("click", () => {
    $$(".subtab").forEach((b) => b.classList.remove("active"));
    $$(".sub-panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    $(`#snap-${btn.dataset.sub}`).classList.add("active");
  });
});

// Sidebar collapse / expand.
$("#sidebar-toggle").addEventListener("click", () => {
  const collapsed = document.body.classList.toggle("sidebar-collapsed");
  $("#sidebar-toggle").textContent = collapsed ? "»" : "«";
  setTimeout(resizeCharts, 0);
});

// ---------------------------------------------------------------------------
// Table renderer
// ---------------------------------------------------------------------------
// opts: { columns, linkStock, signCols, headers, intCols, weekBandCol, footerRow, footerRowClass }
function renderTable(container, rows, opts = {}) {
  const el = $(container);
  if (!rows || rows.length === 0) { el.innerHTML = '<div class="empty">无数据</div>'; return; }
  const cols = opts.columns || Object.keys(rows[0]);
  const linkStock = opts.linkStock;
  const signCols = opts.signCols;
  const headers = opts.headers || {};
  const intCols = opts.intCols;
  const weekBandCol = opts.weekBandCol;

  const head = cols.map((c) =>
    `<th class="${isNumericCol(c) ? "" : "text"}">${headers[c] || c}</th>`).join("");

  // Build one <td> for column `c` with value `v`, reusing the shared formatting
  // rules. `allowLink` gates the K-line link (footer/summary cells never link).
  function cellHtml(c, v, row, allowLink) {
    let cls = isNumericCol(c) ? "" : "text";
    let disp;
    if (RATE_COLS.has(c)) disp = pct(v);
    else if (intCols && intCols.has(c) && typeof v === "number") disp = Math.round(v).toLocaleString();
    else disp = fmt(v);
    const wantSign = signCols ? signCols.has(c)
      : (c.includes("pnl") || c.includes("rate") || c.includes("excess"));
    if (wantSign) cls += signClass(v);
    if (c === "side" && typeof v === "string") cls += v.toLowerCase() === "buy" ? " pos" : " neg";
    if (allowLink && linkStock && (c === "stock_code" || c === "stock_name") && row && row.stock_code) {
      disp = `<a class="klink" data-code="${row.stock_code}">${disp || row.stock_code}</a>`;
    }
    return `<td class="${cls}">${disp}</td>`;
  }

  // Alternating background per adjacent week group (band 0 / band 1).
  let band = 0, prevWeek = null;
  const body = rows.map((row) => {
    let rowCls = "";
    if (weekBandCol) {
      const w = row[weekBandCol];
      if (prevWeek !== null && w !== prevWeek) band ^= 1;
      prevWeek = w;
      rowCls = band ? " wk-band" : "";
    }
    const tds = cols.map((c) => cellHtml(c, row[c], row, true));
    return `<tr class="${rowCls}">${tds.join("")}</tr>`;
  }).join("");

  let foot = "";
  if (opts.footerRow) {
    const fcls = opts.footerRowClass || "sum-row";
    const ftds = cols.map((c) => cellHtml(c, opts.footerRow[c], opts.footerRow, false));
    foot = `<tfoot><tr class="${fcls}">${ftds.join("")}</tr></tfoot>`;
  }

  el.innerHTML = `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody>${foot}</table>`;
  el.querySelectorAll("a.klink").forEach((a) => a.addEventListener("click", () => openKline(a.dataset.code)));
}

// ---------------------------------------------------------------------------
// Snapshot tab
// ---------------------------------------------------------------------------
async function loadSnapshotDates() {
  try {
    const { dates } = await api("/api/dates", { ...accountParams(), table: "live_asset_snapshot" });
    const sel = $("#snap-date");
    sel.innerHTML = dates.map((d) => `<option>${d}</option>`).join("");
    if (dates.length) loadSnapshot(); else renderEmptySnapshot();
  } catch (e) { toast(e.message); }
}
function renderEmptySnapshot() {
  ["#snap-asset", "#snap-positions", "#snap-target", "#snap-orders", "#snap-trades"]
    .forEach((c) => renderTable(c, []));
}
async function loadSnapshot() {
  const date = $("#snap-date").value;
  const phase = $("#snap-phase").value;
  if (!date) return;
  const base = accountParams();
  try {
    const [asset, pos, tgt, orders, trades] = await Promise.all([
      api("/api/asset", { ...base, start: date, end: date, snapshot_type: phase || "after_trading" }),
      api("/api/positions", { ...base, date, snapshot_type: phase }),
      api("/api/target", { ...base, date, snapshot_type: phase }),
      api("/api/orders", { ...base, date }),
      api("/api/trades", { ...base, date }),
    ]);
    renderTable("#snap-asset", asset.rows);
    renderTable("#snap-positions", pos.rows, { linkStock: true });
    renderTable("#snap-target", tgt.rows, { linkStock: true });
    renderTable("#snap-orders", orders.rows, { linkStock: true });
    renderTable("#snap-trades", trades.rows, { linkStock: true });
  } catch (e) { toast(e.message); }
}
$("#snap-refresh").addEventListener("click", loadSnapshot);
$("#snap-date").addEventListener("change", loadSnapshot);
$("#snap-phase").addEventListener("change", loadSnapshot);
let autoTimer = null;
$("#snap-auto").addEventListener("change", (e) => {
  clearInterval(autoTimer);
  if (e.target.checked) autoTimer = setInterval(loadSnapshot, 15000);
});

// ---------------------------------------------------------------------------
// Return report tab
// ---------------------------------------------------------------------------
// Concise Chinese headers for the return report.
const REPORT_HEADERS = {
  trade_date: "日期",
  before_market_value: "盘前市值",
  after_market_value: "盘后市值",
  return_amount: "当日盈亏",
  strat_daily_rate: "策略日收益",
  csi1000_daily_rate: "中证1000日收益",
  excess_daily_rate: "日超额",
  week_label: "周",
  week_cum_return_amount: "本周累计盈亏",
  week_cum_strat_rate: "本周策略累计",
  week_cum_csi1000_rate: "本周中证1000累计",
  week_cum_excess_rate: "本周累计超额",
  buy_slippage_bps: "买入滑点(bp)",
  sell_slippage_bps: "卖出滑点(bp)",
  total_slippage_bps: "总滑点(bp)",
};
// Columns to sign-color (rates, excess, pnl-like amounts, slippage).
const REPORT_SIGN_COLS = new Set([
  "return_amount", "strat_daily_rate", "csi1000_daily_rate", "excess_daily_rate",
  "week_cum_return_amount", "week_cum_strat_rate", "week_cum_csi1000_rate",
  "week_cum_excess_rate", "buy_slippage_bps", "sell_slippage_bps", "total_slippage_bps",
]);
// Amounts shown as whole numbers (no fractional part).
const REPORT_INT_COLS = new Set(["return_amount", "week_cum_return_amount"]);
// Daily columns summed straight across every visible row.
const REPORT_SUM_COLS = ["return_amount", "strat_daily_rate", "csi1000_daily_rate", "excess_daily_rate"];
// Weekly-cumulative columns: already cumulative-to-date within a week, so sum
// only the last row of each week (the week's full total).
const REPORT_WEEK_SUM_COLS = [
  "week_cum_return_amount", "week_cum_strat_rate", "week_cum_csi1000_rate", "week_cum_excess_rate",
];

// Build the bottom summary row directly from the rendered rows (not the backend).
function buildReportSummary(rows) {
  if (!rows || rows.length === 0) return null;
  const sum = { trade_date: "合计" };
  for (const c of REPORT_SUM_COLS) {
    sum[c] = rows.reduce((acc, r) => acc + (typeof r[c] === "number" ? r[c] : 0), 0);
  }
  // Pick the last row per week (rows are chronological), then sum those values.
  const lastByWeek = new Map();
  for (const r of rows) lastByWeek.set(r.week_label, r);
  const weekEnds = [...lastByWeek.values()];
  for (const c of REPORT_WEEK_SUM_COLS) {
    sum[c] = weekEnds.reduce((acc, r) => acc + (typeof r[c] === "number" ? r[c] : 0), 0);
  }
  return sum;
}
async function loadReport() {
  if (!state.account) return;
  const start = $("#report-start").value;
  const end = $("#report-end").value || todayISO();
  if (!start) { toast("请设置起始日期"); return; }
  try {
    const { columns, rows } = await api("/api/returns", { ...accountParams(), start, end });
    // Drop rows lacking both盘前/盘后 market values.
    const kept = rows.filter((r) =>
      r.before_market_value != null && r.after_market_value != null);
    renderTable("#report-table", kept, {
      columns,
      headers: REPORT_HEADERS,
      signCols: REPORT_SIGN_COLS,
      intCols: REPORT_INT_COLS,
      weekBandCol: "week_label",
      footerRow: buildReportSummary(kept),
    });
  } catch (e) { toast(e.message); }
}
$("#report-refresh").addEventListener("click", loadReport);

// ---------------------------------------------------------------------------
// Charts registry
// ---------------------------------------------------------------------------
const charts = {};
function getChart(id) {
  if (!charts[id]) charts[id] = echarts.init(document.getElementById(id), "dark");
  // A chart first init'd while its tab was hidden gets a 0x0 canvas; resize on
  // every access so a now-visible container is measured correctly before draw.
  charts[id].resize();
  return charts[id];
}
function resizeCharts() { Object.values(charts).forEach((c) => c.resize()); }
window.addEventListener("resize", resizeCharts);

function baseLineOption(title) {
  return {
    backgroundColor: "transparent",
    title: { text: title, textStyle: { color: "#d7dce6", fontSize: 14 } },
    tooltip: { trigger: "axis" },
    legend: { textStyle: { color: "#8a94a8" }, top: 26 },
    grid: { left: 66, right: 30, top: 70, bottom: 60 },
    xAxis: { type: "category", axisLabel: { color: "#8a94a8" } },
    yAxis: { type: "value", axisLabel: { color: "#8a94a8" }, splitLine: { lineStyle: { color: "#2a3346" } } },
    dataZoom: [{ type: "inside" }, { type: "slider", height: 18, bottom: 12 }],
    series: [],
  };
}

// ---------------------------------------------------------------------------
// Time-series tab
// ---------------------------------------------------------------------------
const ASSET_METRICS = [
  "total_asset", "market_value", "cash", "available_cash", "frozen_cash",
  "nt_equity", "nt_unrealized_pnl", "nt_realized_pnl",
];
const RETURN_METRICS = [
  "strat_daily_rate", "csi1000_daily_rate", "excess_daily_rate",
  "week_cum_strat_rate", "week_cum_csi1000_rate", "week_cum_excess_rate",
  "return_amount", "week_cum_return_amount", "before_market_value", "after_market_value",
  "buy_slippage_bps", "sell_slippage_bps", "total_slippage_bps",
];
function fillMetricSelect(sel, dataset) {
  const metrics = dataset === "returns" ? RETURN_METRICS : ASSET_METRICS;
  sel.innerHTML = metrics.map((m) => `<option>${m}</option>`).join("");
}
$("#series-dataset").addEventListener("change", (e) => fillMetricSelect($("#series-metric"), e.target.value));

async function fetchSeriesData(dataset, base, start, end) {
  if (dataset === "returns") return (await api("/api/returns", { ...base, start, end })).rows;
  return (await api("/api/asset", { ...base, start, end, snapshot_type: "after_trading" })).rows;
}

$("#series-plot").addEventListener("click", async () => {
  const dataset = $("#series-dataset").value;
  const metric = $("#series-metric").value;
  const start = $("#series-start").value, end = $("#series-end").value;
  if (!start || !end) { toast("请选择起止日期"); return; }
  try {
    const rows = await fetchSeriesData(dataset, accountParams(), start, end);
    const opt = baseLineOption(`${accountLabel(state.account)} · ${metric}`);
    opt.xAxis.data = rows.map((r) => r.trade_date);
    if (RATE_COLS.has(metric)) opt.yAxis.axisLabel.formatter = (v) => (v * 100).toFixed(1) + "%";
    opt.series = [{ name: metric, type: "line", showSymbol: false, connectNulls: true, data: rows.map((r) => r[metric]) }];
    getChart("series-chart").setOption(opt, true);
  } catch (e) { toast(e.message); }
});

// ---------------------------------------------------------------------------
// Comparison tab
// ---------------------------------------------------------------------------
let cmpSeries = []; // [{source, account_id, trader_id, dataset, metric, label}]
let activePreset = null; // name of the currently-selected saved comparison

function populateCompareSelectors() {
  const srcSel = $(".cmp-source");
  srcSel.innerHTML = $("#sel-source").innerHTML;
  refreshCompareAccounts();
  srcSel.onchange = refreshCompareAccounts;
  $(".cmp-dataset").onchange = () => fillMetricSelect($(".cmp-metric"), $(".cmp-dataset").value);
  fillMetricSelect($(".cmp-metric"), $(".cmp-dataset").value);
  // Default window: end = today, start = one month ago (only when blank).
  if (!$("#cmp-end").value) $("#cmp-end").value = iso(new Date());
  if (!$("#cmp-start").value) $("#cmp-start").value = monthsBefore(null, 1);
  loadSavedPresets();
}
// Changing either date re-draws immediately if there are series to plot.
["#cmp-start", "#cmp-end"].forEach((sel) => {
  $(sel).addEventListener("change", () => { if (cmpSeries.length) plotCompare(); });
});
async function refreshCompareAccounts() {
  const src = $(".cmp-source").value;
  try {
    const { accounts } = await api("/api/accounts", { source: src });
    $(".cmp-account").innerHTML = accounts
      .map((a) => `<option value="${a.account_id}|${a.trader_id}">${a.account_id} / ${a.trader_id}</option>`).join("");
  } catch (e) { toast(e.message); }
}
$(".cmp-add").addEventListener("click", () => {
  const source = $(".cmp-source").value;
  const [account_id, trader_id] = $(".cmp-account").value.split("|");
  const dataset = $(".cmp-dataset").value;
  const metric = $(".cmp-metric").value;
  cmpSeries.push({ source, account_id, trader_id, dataset, metric,
    label: `${source}·${account_id}/${trader_id}·${metric}` });
  activePreset = null; // manual edit deselects any saved comparison
  highlightSavedPreset();
  renderCmpList();
});
function renderCmpList() {
  $("#cmp-list").innerHTML = cmpSeries
    .map((s, i) => `<li><span>${s.label}</span><button data-i="${i}">✕</button></li>`).join("");
  $("#cmp-list").querySelectorAll("button").forEach((b) => {
    b.onclick = () => {
      cmpSeries.splice(Number(b.dataset.i), 1);
      activePreset = null; highlightSavedPreset();
      renderCmpList();
    };
  });
}
async function plotCompare() {
  const start = $("#cmp-start").value, end = $("#cmp-end").value;
  if (!start || !end) { toast("请选择起止日期"); return; }
  if (cmpSeries.length === 0) { toast("请先添加序列"); return; }
  try {
    const results = await Promise.all(cmpSeries.map((s) =>
      fetchSeriesData(s.dataset, { source: s.source, account: s.account_id, trader: s.trader_id }, start, end)
        .then((rows) => ({ s, rows }))));
    const dateSet = new Set();
    results.forEach(({ rows }) => rows.forEach((r) => dateSet.add(r.trade_date)));
    const dates = Array.from(dateSet).sort();
    const anyRate = cmpSeries.some((s) => RATE_COLS.has(s.metric));
    const opt = baseLineOption("对比");
    opt.xAxis.data = dates;
    if (anyRate) opt.yAxis.axisLabel.formatter = (v) => (v * 100).toFixed(1) + "%";
    // Wrapping, scrollable legend so long series names never overlap.
    opt.legend = {
      type: "scroll", top: 30, left: 10, right: 10,
      textStyle: { color: "#8a94a8" },
    };
    // Give the legend room to spill onto multiple rows before the plot starts.
    const legendRows = Math.min(4, Math.ceil(cmpSeries.length / 2));
    opt.grid.top = 40 + legendRows * 20;
    // Tooltip values keep 4 decimals (rates shown as %).
    opt.tooltip.valueFormatter = (v) => {
      if (v === null || v === undefined) return "-";
      return anyRate ? (v * 100).toFixed(4) + "%" : Number(v).toFixed(4);
    };
    opt.series = results.map(({ s, rows }) => {
      const map = new Map(rows.map((r) => [r.trade_date, r[s.metric]]));
      return { name: s.label, type: "line", showSymbol: false, connectNulls: true,
        data: dates.map((d) => (map.has(d) ? map.get(d) : null)) };
    });
    getChart("cmp-chart").setOption(opt, true);
  } catch (e) { toast(e.message); }
}
$("#cmp-plot").addEventListener("click", plotCompare);

// ---- saved presets (server-persisted) ----
$("#cmp-save").addEventListener("click", async () => {
  const name = $("#cmp-name").value.trim();
  if (!name) { toast("请输入对比名称"); return; }
  if (cmpSeries.length === 0) { toast("当前没有序列"); return; }
  try {
    await api("/api/compare_presets", {}, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, series: cmpSeries }),
    });
    activePreset = name; // the just-saved comparison is now the active one
    $("#cmp-name").value = "";
    toast("已保存");
    loadSavedPresets();
  } catch (e) { toast(e.message); }
});
function highlightSavedPreset() {
  $("#cmp-saved").querySelectorAll("li[data-name]").forEach((li) => {
    const name = decodeURIComponent(li.dataset.name);
    li.classList.toggle("active", name === activePreset);
  });
}
async function loadSavedPresets() {
  try {
    const { presets } = await api("/api/compare_presets");
    $("#cmp-saved").innerHTML = presets.length
      ? presets.map((p) => `<li data-name="${encodeURIComponent(p.name)}"><span class="name">${p.name}</span><button class="del">✕</button></li>`).join("")
      : '<li class="empty">暂无</li>';
    $("#cmp-saved").querySelectorAll("li[data-name]").forEach((li) => {
      const name = decodeURIComponent(li.dataset.name);
      const preset = presets.find((p) => p.name === name);
      li.querySelector(".name").onclick = () => {
        cmpSeries = preset.series.slice();
        activePreset = name; highlightSavedPreset();
        renderCmpList(); plotCompare();
      };
      li.querySelector(".del").onclick = async (ev) => {
        ev.stopPropagation();
        try {
          await api("/api/compare_presets", { name }, { method: "DELETE" });
          if (activePreset === name) activePreset = null;
          loadSavedPresets();
        } catch (e) { toast(e.message); }
      };
    });
    highlightSavedPreset();
  } catch (e) { toast(e.message); }
}

// ---------------------------------------------------------------------------
// K-line tab
// ---------------------------------------------------------------------------
const iso = (d) => d.toISOString().slice(0, 10);
function monthsBefore(dateStr, n) {
  const d = dateStr ? new Date(dateStr) : new Date();
  d.setMonth(d.getMonth() - n);
  return iso(d);
}
// Default k-line window: end = today, start = 6 months ago (only when blank).
function defaultKlineRange() {
  if (!$("#kline-end").value) $("#kline-end").value = iso(new Date());
  if (!$("#kline-start").value) $("#kline-start").value = monthsBefore(null, 6);
}
// Jumping from a snapshot link: anchor the window on the snapshot's date —
// end = that date, start = 6 months before it.
function openKline(stockCode) {
  $("#kline-code").value = stockCode;
  const anchor = $("#snap-date").value;
  if (anchor) {
    $("#kline-end").value = anchor;
    $("#kline-start").value = monthsBefore(anchor, 6);
  } else {
    defaultKlineRange();
  }
  $$(".nav-item").forEach((b) => b.classList.remove("active"));
  $$(".tab-panel").forEach((p) => p.classList.remove("active"));
  document.querySelector('.nav-item[data-tab="kline"]').classList.add("active");
  $("#tab-kline").classList.add("active");
  setTimeout(plotKline, 0);
}
async function plotKline() {
  const code = $("#kline-code").value.trim();
  const start = $("#kline-start").value, end = $("#kline-end").value;
  const withMarks = $("#kline-marks").checked;
  if (!code || !start || !end) { toast("请填写代码与起止日期"); return; }
  try {
    let bars, trades = [], name = "";
    if (withMarks && state.account) {
      const d = await api("/api/kline_with_trades", { ...accountParams(), stock_code: code, start, end });
      bars = d.bars; trades = d.trades; name = d.stock_name;
    } else {
      const d = await api("/api/kline", { stock_code: code, start, end, source: state.source });
      bars = d.rows; name = d.stock_name;
    }
    if (!bars || bars.length === 0) { toast("ClickHouse 无该股票行情"); return; }

    const dates = bars.map((b) => String(b.ts).slice(0, 10));
    const candle = bars.map((b) => [b.open, b.close, b.low, b.high]);
    const volume = bars.map((b) => b.volume);

    // Day-over-day 涨跌幅 (a-share: %) from previous close.
    const priceRange = { min: Infinity, max: -Infinity };
    bars.forEach((b) => {
      if (b.low < priceRange.min) priceRange.min = b.low;
      if (b.high > priceRange.max) priceRange.max = b.high;
    });
    const pctChange = bars.map((b, i) => {
      if (i === 0) return null;
      const prev = bars[i - 1].close;
      return prev ? ((b.close - prev) / prev) * 100 : null;
    });

    // Aggregate fills per day → { date: {buy:{qty,pxQty,amt}, sell:{...}} }.
    const dayFills = {};
    trades.forEach((t) => {
      const d = String(t.trade_date).slice(0, 10);
      const px = Number(t.price) || 0, qty = Number(t.quantity) || 0;
      const amt = Number(t.amount) != null && !isNaN(Number(t.amount)) && Number(t.amount) !== 0
        ? Number(t.amount) : px * qty;
      const side = String(t.side).toLowerCase() === "buy" ? "buy" : "sell";
      const day = dayFills[d] || (dayFills[d] = {
        buy: { qty: 0, pxQty: 0, amt: 0 }, sell: { qty: 0, pxQty: 0, amt: 0 },
      });
      day[side].qty += qty; day[side].pxQty += px * qty; day[side].amt += amt;
    });

    // High-contrast markers: blue up-arrow for buy (above the high), orange
    // down-pin for sell (below the low), each with a white border + price label.
    // Anchor exactly at the high/low and push the whole symbol clear with a
    // fixed pixel offset so it never covers the candle regardless of zoom.
    const SYM = 16, GAP = 6; // symbol size + clearance, in pixels
    const markData = trades.map((t) => {
      const d = String(t.trade_date).slice(0, 10);
      const isBuy = String(t.side).toLowerCase() === "buy";
      const idx = dates.indexOf(d);
      const anchor = isBuy ? (bars[idx] ? bars[idx].high : t.price) : (bars[idx] ? bars[idx].low : t.price);
      // Negative y-offset moves up (toward the top of the chart).
      const yOffset = isBuy ? -(SYM / 2 + GAP) : (SYM / 2 + GAP);
      return {
        name: isBuy ? "买" : "卖", coord: [d, Number(anchor)],
        value: `${isBuy ? "买" : "卖"} ${fmt(t.quantity)}@${fmt(t.price)}`,
        symbol: isBuy ? "arrow" : "pin", symbolRotate: isBuy ? 0 : 180,
        symbolSize: SYM, symbolOffset: [0, yOffset],
        itemStyle: { color: isBuy ? "#4c8dff" : "#ffa726", borderColor: "#fff", borderWidth: 1 },
        label: {
          show: true, formatter: isBuy ? "买" : "卖", color: "#fff",
          fontSize: 9, position: isBuy ? "top" : "bottom",
          distance: 3, backgroundColor: "transparent",
        },
      };
    });

    const title = name ? `${code} ${name} 日K线` : `${code} 日K线`;

    const tipHtml = (ps) => {
      const p = ps[0];
      const i = p.dataIndex;
      const c = candle[i];
      const chg = pctChange[i];
      const rows = [
        `<b>${p.axisValue}</b>`,
        `开盘 ${fmt(c[0])}　收盘 ${fmt(c[1])}`,
        `最高 ${fmt(c[3])}　最低 ${fmt(c[2])}`,
      ];
      if (chg != null) {
        const cls = chg > 0 ? "pos" : chg < 0 ? "neg" : "";
        rows.push(`涨跌幅 <span class="${cls}">${chg >= 0 ? "+" : ""}${chg.toFixed(2)}%</span>`);
      }
      rows.push(`成交量 ${fmt(volume[i])}`);
      // Per-day fills for this account, visually set apart from the bar block.
      const day = dayFills[dates[i]];
      if (day) {
        const parts = [];
        if (day.buy.qty) {
          const avg = day.buy.pxQty / day.buy.qty;
          parts.push(`<span style="color:#4c8dff">买 均价 ${fmt(avg)}　量 ${fmt(day.buy.qty)}　额 ${fmt(day.buy.amt)}</span>`);
        }
        if (day.sell.qty) {
          const avg = day.sell.pxQty / day.sell.qty;
          parts.push(`<span style="color:#ffa726">卖 均价 ${fmt(avg)}　量 ${fmt(day.sell.qty)}　额 ${fmt(day.sell.amt)}</span>`);
        }
        if (parts.length) {
          rows.push('<div style="border-top:1px dashed #4c5468;margin:4px 0 2px"></div>' + parts.join("<br/>"));
        }
      }
      return rows.join("<br/>");
    };

    const opt = {
      backgroundColor: "transparent",
      title: {
        text: title, left: 10,
        textStyle: { color: "#d7dce6", fontSize: 14 },
      },
      graphic: [],
      tooltip: { trigger: "axis", axisPointer: { type: "cross" }, formatter: tipHtml, confine: true },
      legend: { data: ["K线", "成交量"], textStyle: { color: "#8a94a8" }, left: 10, top: 26 },
      axisPointer: { link: [{ xAxisIndex: "all" }] },
      grid: [{ left: 66, right: 30, top: 74, height: "56%" }, { left: 66, right: 30, top: "73%", height: "16%" }],
      xAxis: [
        { type: "category", data: dates, axisLabel: { color: "#8a94a8" }, boundaryGap: true },
        { type: "category", gridIndex: 1, data: dates, axisLabel: { show: false } },
      ],
      yAxis: [
        { scale: true, axisLabel: { color: "#8a94a8" }, splitLine: { lineStyle: { color: "#2a3346" } } },
        { scale: true, gridIndex: 1, axisLabel: { show: false }, splitLine: { show: false } },
      ],
      dataZoom: [
        { type: "inside", xAxisIndex: [0, 1] },
        { type: "slider", xAxisIndex: [0, 1], height: 18, bottom: 8 },
      ],
      series: [
        {
          name: "K线", type: "candlestick", data: candle,
          itemStyle: { color: "#ef5350", color0: "#26a69a", borderColor: "#ef5350", borderColor0: "#26a69a" },
          markPoint: withMarks ? { data: markData, tooltip: { formatter: (p) => p.data.value } } : undefined,
        },
        { name: "成交量", type: "bar", xAxisIndex: 1, yAxisIndex: 1, data: volume, itemStyle: { color: "#4c8dff88" } },
      ],
    };
    getChart("kline-chart").setOption(opt, true);
  } catch (e) { toast(e.message); }
}
$("#kline-plot").addEventListener("click", plotKline);

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
fillMetricSelect($("#series-metric"), "asset");
initSources().catch((e) => toast("初始化失败: " + e.message));
