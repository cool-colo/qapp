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

// A styled modal confirm — resolves true (confirm) / false (cancel/backdrop/Esc).
// Replaces window.confirm for a cleaner look. Supports HTML body + danger style.
function confirmDialog({ title = "请确认", bodyHtml = "", okText = "确定", cancelText = "取消", danger = false } = {}) {
  return new Promise((resolve) => {
    const back = document.createElement("div");
    back.className = "modal-backdrop";
    back.innerHTML =
      `<div class="modal" role="dialog" aria-modal="true">
         <div class="modal-title">${title}</div>
         <div class="modal-body">${bodyHtml}</div>
         <div class="modal-actions">
           <button class="ghost modal-cancel">${cancelText}</button>
           <button class="${danger ? "danger" : ""} modal-ok">${okText}</button>
         </div>
       </div>`;
    document.body.appendChild(back);
    const done = (val) => { window.removeEventListener("keydown", onKey); back.remove(); resolve(val); };
    const onKey = (e) => { if (e.key === "Escape") done(false); if (e.key === "Enter") done(true); };
    back.querySelector(".modal-ok").addEventListener("click", () => done(true));
    back.querySelector(".modal-cancel").addEventListener("click", () => done(false));
    back.addEventListener("mousedown", (e) => { if (e.target === back) done(false); });
    window.addEventListener("keydown", onKey);
    requestAnimationFrame(() => back.classList.add("show"));
    back.querySelector(".modal-ok").focus();
  });
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
  if (typeof v !== "number") return String(v);  // sentinel like "-"
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
  else if (tab === "realtime") loadRealtime();
  else if (tab === "control") loadControl();
}

// ---------------------------------------------------------------------------
// Sidebar nav + snapshot sub-tabs
// ---------------------------------------------------------------------------
$$(".nav-item").forEach((btn) => {
  btn.addEventListener("click", () => {
    // Fold every group, then open only the one this item belongs to (if any).
    const group = btn.closest(".nav-group");
    $$(".nav-group").forEach((g) => g.classList.toggle("open", g === group));
    $$(".nav-item").forEach((b) => b.classList.remove("active"));
    $$(".tab-panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    $(`#tab-${btn.dataset.tab}`).classList.add("active");
    if (btn.dataset.tab === "report") loadReport();
    else if (btn.dataset.tab === "realtime") loadRealtime();
    else if (btn.dataset.tab === "control") loadControl();
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
// Table renderer — with per-column click-to-sort + a per-column filter row.
// ---------------------------------------------------------------------------
// opts: { columns, linkStock, signCols, headers, intCols, rateCols, weekBandCol,
//         footerRow, footerFn, footerRowClass, cellFn, noSort, noFilter, onRender }
//   footerFn(visibleRows) -> rowObject|null   dynamic footer recomputed per view
//   cellFn(col, value, row) -> tdHtml|undefined   per-cell override (undefined = default)
//   rateCols: Set of columns formatted as percentages
//   noSort / noFilter: Sets of columns excluded from sort / filter
//   onRender(el): called after every (re)draw so callers can (re)bind handlers
function isNumericColFor(c, opts) {
  return isNumericCol(c)
    || (opts.intCols && opts.intCols.has(c))
    || (opts.rateCols && opts.rateCols.has(c))
    || RATE_COLS.has(c);
}

// The string a cell displays — shared by the cell renderer and the filter so
// what you type against matches what you see.
function cellDisplay(c, v, opts) {
  if (RATE_COLS.has(c) || (opts.rateCols && opts.rateCols.has(c))) return pct(v);
  if (opts.intCols && opts.intCols.has(c) && typeof v === "number") return Math.round(v).toLocaleString();
  return fmt(v);
}

// A column's filter is an object { text, values }:
//   text   — substring / numeric-comparator query ("" = no text filter)
//   values — Set of chosen displayed-string keys, or null for "all values"
// A column is "active" when it has text or a value-set.
function filterActive(f) {
  return !!f && ((f.text && f.text.trim()) || f.values !== null && f.values !== undefined);
}

// Match the free-text part. Numeric columns honor >,>=,<,<=,= prefixes;
// everything else is a case-insensitive substring on the displayed string.
function passesText(c, v, text, opts) {
  const q = (text || "").trim();
  if (!q) return true;
  if (isNumericColFor(c, opts) && typeof v === "number") {
    const m = q.match(/^(>=|<=|>|<|=)\s*(-?\d[\d,]*\.?\d*)$/);
    if (m) {
      const n = parseFloat(m[2].replace(/,/g, ""));
      if (!isNaN(n)) {
        switch (m[1]) {
          case ">": return v > n;
          case ">=": return v >= n;
          case "<": return v < n;
          case "<=": return v <= n;
          case "=": return v === n;
        }
      }
    }
  }
  return cellDisplay(c, v, opts).toLowerCase().includes(q.toLowerCase());
}

// Row passes a column filter iff it's within the chosen value-set (or all) AND
// matches the free-text query.
function passesFilter(c, v, f, opts) {
  if (!filterActive(f)) return true;
  if (f.values !== null && f.values !== undefined && !f.values.has(cellDisplay(c, v, opts))) return false;
  return passesText(c, v, f.text, opts);
}

function renderTable(container, rows, opts = {}) {
  const el = $(container);
  if (!rows || rows.length === 0) { el.innerHTML = '<div class="empty">无数据</div>'; return; }
  const cols = opts.columns || Object.keys(rows[0]);
  const linkStock = opts.linkStock;
  const signCols = opts.signCols;
  const headers = opts.headers || {};
  const weekBandCol = opts.weekBandCol;
  const noSort = opts.noSort || new Set();
  const noFilter = opts.noFilter || new Set();

  // Persist sort/filter state across redraws so re-sorting keeps prior filters
  // and vice-versa. Re-render (new data) resets neither col choice nor filters.
  const prev = el._tableState || {};
  // Default sort applies only on first render (no prior user choice persisted).
  const ds = opts.defaultSort || {};
  const state = {
    rows, opts, cols,
    sortCol: prev.sortCol || ds.col || null,
    sortDir: prev.sortDir || ds.dir || null,
    filters: prev.filters || {},
  };
  el._tableState = state;

  // Build one <td> for column `c`. `allowLink` gates the K-line link (footer
  // cells never link). `cellFn` may fully override a cell (e.g. action button).
  function cellHtml(c, v, row, allowLink) {
    if (opts.cellFn) {
      const custom = opts.cellFn(c, v, row);
      if (custom !== undefined) return custom;
    }
    let cls = isNumericColFor(c, opts) ? "" : "text";
    let disp = cellDisplay(c, v, opts);
    const wantSign = signCols ? signCols.has(c)
      : (c.includes("pnl") || c.includes("rate") || c.includes("excess"));
    if (wantSign) cls += signClass(v);
    if (c === "side" && typeof v === "string") cls += v.toLowerCase() === "buy" ? " pos" : " neg";
    if (allowLink && linkStock && (c === "stock_code" || c === "stock_name") && row && row.stock_code) {
      disp = `<a class="klink" data-code="${row.stock_code}">${disp || row.stock_code}</a>`;
    }
    return `<td class="${cls}">${disp}</td>`;
  }

  // ---- header row: label + sort caret + a hover/active filter funnel icon ----
  const FUNNEL = '<svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true"><path fill="currentColor" d="M1.5 2h13l-5 6v5l-3 1.5V8z"/></svg>';
  const headCells = cols.map((c) => {
    const numeric = isNumericColFor(c, opts);
    const sortable = !noSort.has(c);
    const label = headers[c] || c;
    const caret = sortable ? '<span class="sort-caret"></span>' : "";
    const funnel = !noFilter.has(c)
      ? `<button class="filter-btn" data-filter-col="${c}" title="筛选">${FUNNEL}</button>` : "";
    const dataCol = sortable ? ` data-col="${c}"` : "";
    const role = sortable ? ' role="button"' : "";
    return `<th class="${numeric ? "" : "text"}"${dataCol}${role}><span class="th-label">${label}${caret}</span>${funnel}</th>`;
  }).join("");
  el.innerHTML =
    `<table><thead><tr class="head-row">${headCells}</tr></thead>` +
    `<tbody></tbody><tfoot></tfoot></table>`;
  const table = el.querySelector("table");

  function draw() {
    // 1. filter
    let view = state.rows.filter((row) =>
      cols.every((c) => (state.filters[c] ? passesFilter(c, row[c], state.filters[c], opts) : true)));
    // 2. sort (nulls/blanks always to the bottom)
    if (state.sortCol) {
      const c = state.sortCol, dir = state.sortDir === "desc" ? -1 : 1;
      const numeric = isNumericColFor(c, opts);
      const isEmpty = (x) => x === null || x === undefined || x === "" || (numeric && x === "-");
      view = view.slice().sort((ra, rb) => {
        const a = ra[c], b = rb[c];
        if (isEmpty(a) && isEmpty(b)) return 0;
        if (isEmpty(a)) return 1;
        if (isEmpty(b)) return -1;
        if (numeric) return (Number(a) - Number(b)) * dir;
        return String(a).localeCompare(String(b)) * dir;
      });
    }
    // 3. body (+ week bands over the visible rows)
    let band = 0, prevWeek = null;
    table.querySelector("tbody").innerHTML = view.map((row) => {
      let rowCls = "";
      if (weekBandCol) {
        const w = row[weekBandCol];
        if (prevWeek !== null && w !== prevWeek) band ^= 1;
        prevWeek = w;
        rowCls = band ? " wk-band" : "";
      }
      if (opts.rowClass) {
        const extra = opts.rowClass(row);
        if (extra) rowCls += " " + extra;
      }
      return `<tr class="${rowCls}">${cols.map((c) => cellHtml(c, row[c], row, true)).join("")}</tr>`;
    }).join("");
    // 4. footer (recomputed from the visible rows when a footerFn is given)
    const footerRow = opts.footerFn ? opts.footerFn(view) : opts.footerRow;
    const fcls = opts.footerRowClass || "sum-row";
    table.querySelector("tfoot").innerHTML = footerRow
      ? `<tr class="${fcls}">${cols.map((c) => cellHtml(c, footerRow[c], footerRow, false)).join("")}</tr>`
      : "";
    // 5. sort carets
    table.querySelectorAll("th[data-col]").forEach((th) => {
      const caret = th.querySelector(".sort-caret");
      if (!caret) return;
      const active = th.dataset.col === state.sortCol;
      th.classList.toggle("sorted", active);
      caret.textContent = active ? (state.sortDir === "desc" ? " ▼" : " ▲") : "";
    });
    // 5b. keep the funnel lit on columns that have an active filter
    table.querySelectorAll("th .filter-btn").forEach((btn) => {
      btn.parentElement.classList.toggle("has-filter", filterActive(state.filters[btn.dataset.filterCol]));
    });
    // 6. rebind row-level handlers (klinks always; caller extras via onRender)
    el.querySelectorAll("a.klink").forEach((a) => a.addEventListener("click", () => openKline(a.dataset.code)));
    if (opts.onRender) opts.onRender(el);
  }

  // Header click → asc → desc → clear. Clicks on the funnel are excluded so
  // opening the filter dialog never re-sorts the column.
  table.querySelectorAll("th[data-col]").forEach((th) => {
    th.addEventListener("click", (e) => {
      if (e.target.closest(".filter-btn")) return;
      const c = th.dataset.col;
      if (state.sortCol !== c) { state.sortCol = c; state.sortDir = "asc"; }
      else if (state.sortDir === "asc") state.sortDir = "desc";
      else { state.sortCol = null; state.sortDir = null; }
      draw();
    });
  });
  // Funnel icon → filter dialog for that column.
  table.querySelectorAll(".filter-btn").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      openFilterPopup(btn.closest("th"), btn.dataset.filterCol, state, opts, draw);
    });
  });

  draw();
}

// ---------------------------------------------------------------------------
// Column filter dialog — a single reusable floating popup (Excel-style):
// a distinct-value checklist plus a free-text / comparator box.
// ---------------------------------------------------------------------------
let _fpEl = null;      // the shared popup element
let _fpClose = null;   // active close handler (outside-click / esc / scroll)

function closeFilterPopup() {
  if (_fpEl) _fpEl.style.display = "none";
  if (_fpClose) {
    document.removeEventListener("mousedown", _fpClose, true);
    document.removeEventListener("keydown", _fpClose, true);
    window.removeEventListener("scroll", _fpClose, true);
    window.removeEventListener("resize", _fpClose, true);
    _fpClose = null;
  }
}

function openFilterPopup(th, col, state, opts, draw) {
  closeFilterPopup();
  if (!_fpEl) {
    _fpEl = document.createElement("div");
    _fpEl.className = "filter-popup";
    document.body.appendChild(_fpEl);
  }
  const cur = state.filters[col] || { text: "", values: null };

  // Distinct displayed values from the full (unfiltered) row set.
  const seen = new Set();
  const distinct = [];
  for (const r of state.rows) {
    const disp = cellDisplay(col, r[col], opts);
    if (disp === "" || seen.has(disp)) continue;
    seen.add(disp);
    distinct.push({ key: disp, raw: r[col] });
  }
  const numeric = isNumericColFor(col, opts);
  distinct.sort((a, b) =>
    numeric && typeof a.raw === "number" && typeof b.raw === "number"
      ? a.raw - b.raw : a.key.localeCompare(b.key));

  const checked = cur.values; // Set or null(=all)
  _fpEl.innerHTML =
    `<input class="fp-text" placeholder="如 >1000 或 文字" value="${(cur.text || "").replace(/"/g, "&quot;")}">` +
    `<input class="fp-search" placeholder="搜索选项">` +
    `<label class="fp-all"><input type="checkbox" class="fp-all-cb"> (全选)</label>` +
    `<div class="fp-list">` +
    distinct.map((d, i) =>
      `<label data-key="${encodeURIComponent(d.key)}"><input type="checkbox" class="fp-cb" data-i="${i}"` +
      `${checked === null || checked.has(d.key) ? " checked" : ""}> ${d.key}</label>`).join("") +
    `</div>` +
    `<div class="fp-actions">` +
    `<button class="fp-clear">清除</button>` +
    `<button class="fp-cancel">取消</button>` +
    `<button class="fp-ok">确定</button>` +
    `</div>`;

  // Position under the header, clamped to the viewport.
  _fpEl.style.display = "block";
  const r = th.getBoundingClientRect();
  const w = _fpEl.offsetWidth, h = _fpEl.offsetHeight;
  let left = Math.min(r.left, window.innerWidth - w - 8);
  let top = r.bottom + 2;
  if (top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - 2);
  _fpEl.style.left = Math.max(8, left) + "px";
  _fpEl.style.top = top + "px";

  const boxes = () => Array.from(_fpEl.querySelectorAll(".fp-cb"));
  const allCb = _fpEl.querySelector(".fp-all-cb");
  const syncAll = () => {
    const vis = boxes().filter((b) => b.closest("label").style.display !== "none");
    const on = vis.filter((b) => b.checked).length;
    allCb.checked = on === vis.length && vis.length > 0;
    allCb.indeterminate = on > 0 && on < vis.length;
  };
  syncAll();

  allCb.addEventListener("change", () => {
    boxes().forEach((b) => { if (b.closest("label").style.display !== "none") b.checked = allCb.checked; });
  });
  boxes().forEach((b) => b.addEventListener("change", syncAll));

  // In-popup search narrows the checklist (not the table).
  _fpEl.querySelector(".fp-search").addEventListener("input", (e) => {
    const q = e.target.value.trim().toLowerCase();
    _fpEl.querySelectorAll(".fp-list label").forEach((lab) => {
      const key = decodeURIComponent(lab.dataset.key).toLowerCase();
      lab.style.display = key.includes(q) ? "" : "none";
    });
    syncAll();
  });

  const apply = () => {
    const text = _fpEl.querySelector(".fp-text").value;
    const on = boxes().filter((b) => b.checked).map((b) => distinct[Number(b.dataset.i)].key);
    // All checked → "all" (null) so we don't carry a redundant full set.
    const values = on.length === distinct.length ? null : new Set(on);
    state.filters[col] = { text, values };
    closeFilterPopup();
    draw();
  };
  _fpEl.querySelector(".fp-ok").addEventListener("click", apply);
  _fpEl.querySelector(".fp-text").addEventListener("keydown", (e) => { if (e.key === "Enter") apply(); });
  _fpEl.querySelector(".fp-clear").addEventListener("click", () => {
    state.filters[col] = { text: "", values: null };
    closeFilterPopup();
    draw();
  });
  _fpEl.querySelector(".fp-cancel").addEventListener("click", closeFilterPopup);

  _fpEl.querySelector(".fp-text").focus();

  // Dismiss on outside-click, Esc, or scroll/resize (position would drift).
  _fpClose = (e) => {
    if (e.type === "keydown" && e.key !== "Escape") return;
    if (e.type === "mousedown" && _fpEl.contains(e.target)) return;
    closeFilterPopup();
  };
  setTimeout(() => {
    document.addEventListener("mousedown", _fpClose, true);
    document.addEventListener("keydown", _fpClose, true);
    window.addEventListener("scroll", _fpClose, true);
    window.addEventListener("resize", _fpClose, true);
  }, 0);
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
    // Row filtering (drop dates lacking both 盘前/盘后 market values) is done in
    // SQL so the weekly cumulative columns stay consistent with the visible rows.
    renderTable("#report-table", rows, {
      columns,
      headers: REPORT_HEADERS,
      signCols: REPORT_SIGN_COLS,
      intCols: REPORT_INT_COLS,
      weekBandCol: "week_label",
      footerFn: buildReportSummary,
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
  $$(".nav-group").forEach((g) => g.classList.remove("open"));
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
// Realtime info tab (实时信息) — live positions from the node's control API
// ---------------------------------------------------------------------------
// Column order + Chinese headers mirror the broker 持仓 screen.
const RT_COLS = [
  "account", "stock_code", "stock_name", "volume", "can_use_volume", "frozen",
  "avg_price", "last_price", "unrealized_pnl", "pnl_ratio", "day_change",
  "day_pnl", "market_value", "position_cost", "action",
];
const RT_HEADERS = {
  account: "资金账号", stock_code: "证券代码", stock_name: "证券名称",
  volume: "当前拥股", can_use_volume: "可用数量", frozen: "冻结数量",
  avg_price: "成本价", last_price: "最新价", unrealized_pnl: "持仓盈亏",
  pnl_ratio: "盈亏比例", day_change: "当日涨幅", day_pnl: "当日盈亏",
  market_value: "市值", position_cost: "持仓成本", action: "操作",
};
const RT_SIGN_COLS = new Set(["unrealized_pnl", "pnl_ratio", "day_change", "day_pnl"]);
// Ratios rendered as percentages.
const RT_RATE_COLS = new Set(["pnl_ratio", "day_change"]);
const RT_INT_COLS = new Set(["volume", "can_use_volume", "frozen"]);

// The per-row 卖出 button cell (the only custom cell in the realtime table).
function rtActionCell(row) {
  // No sell button when there is nothing available to sell (可用数量 == 0),
  // which also covers sold-out (closed-today) rows.
  if (!row || !row.stock_code || !row.can_use_volume) return "<td></td>";
  return `<td class="text"><button class="rt-sell" data-code="${row.stock_code}" data-name="${row.name || ""}" data-qty="${row.can_use_volume}">卖出</button></td>`;
}

// 合计 (total) row over the *visible* positions (blank cells count as 0). 市值
// prefers the account's broker market_value when the node reports one AND no
// filter is narrowing the view; otherwise it sums what's shown.
function buildRtFooter(rows, asset, filtered) {
  if (!rows || rows.length === 0) return null;
  const a = asset || {};
  const sumCol = (c) => rows.reduce((acc, r) => acc + (typeof r[c] === "number" ? r[c] : 0), 0);
  return {
    account: "合计",
    market_value: (!filtered && typeof a.market_value === "number") ? a.market_value : sumCol("market_value"),
    position_cost: sumCol("position_cost"),
    unrealized_pnl: sumCol("unrealized_pnl"),
    day_pnl: sumCol("day_pnl"),
  };
}

function renderRealtime(positions, asset) {
  const el = $("#rt-positions");
  if (!positions || positions.length === 0) { el.innerHTML = '<div class="empty">无持仓</div>'; return; }
  // The node payload uses `name` for 证券名称; the table column is `stock_name`.
  positions.forEach((p) => { if (p.stock_name === undefined) p.stock_name = p.name; });
  const total = positions.length;
  renderTable("#rt-positions", positions, {
    columns: RT_COLS,
    headers: RT_HEADERS,
    linkStock: true,       // 证券代码 / 证券名称 → K 线图
    signCols: RT_SIGN_COLS,
    rateCols: RT_RATE_COLS,
    intCols: RT_INT_COLS,
    noSort: new Set(["action"]),
    noFilter: new Set(["action"]),
    cellFn: (c, v, row) => (c === "action" ? rtActionCell(row) : undefined),
    rowClass: (row) => (row.closed_today ? "closed-today" : ""),
    defaultSort: { col: "volume", dir: "desc" },
    footerFn: (view) => buildRtFooter(view, asset, view.length !== total),
    onRender: (root) => root.querySelectorAll("button.rt-sell")
      .forEach((b) => b.addEventListener("click", () => sellStock(b.dataset.code, b.dataset.name, b.dataset.qty))),
  });
}

async function sellStock(code, name, qty) {
  const title = "确认卖出";
  const body = `即将市价卖出<br><b>${name ? name + " " : ""}${code}</b>`
    + (qty ? `<br>可用数量 <b>${Number(qty).toLocaleString()}</b> 股` : "");
  const ok = await confirmDialog({ title, bodyHtml: body, okText: "卖出", danger: true });
  if (!ok) return;
  try {
    const r = await api("/api/control/sell", {}, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ account: state.account.account_id, trader: state.account.trader_id, stock_code: code }),
    });
    toast(r && r.ok ? `已提交卖出 ${code}` : "已提交");
    loadRealtime();
  } catch (e) { toast(e.message); }
}

// The realtime/control tabs need a live-node control API for the selected account.
function hasNodeApi() { return !!(state.account && state.account.has_node_api); }
function nodeApiHint(container) {
  $(container).innerHTML = '<div class="empty">该账户未配置实盘节点 API（node_api）</div>';
}

async function loadRealtime() {
  if (!state.account) return;
  if (!hasNodeApi()) { nodeApiHint("#rt-positions"); $("#rt-updated").textContent = ""; return; }
  try {
    const { positions, asset } = await api("/api/realtime/positions",
      { account: state.account.account_id, trader: state.account.trader_id });
    renderRealtime(positions, asset);
    $("#rt-updated").textContent = "更新于 " + new Date().toLocaleTimeString();
  } catch (e) {
    $("#rt-positions").innerHTML = '<div class="empty">获取失败</div>';
    toast(e.message);
  }
}
$("#rt-refresh").addEventListener("click", loadRealtime);
let rtTimer = null;
$("#rt-auto").addEventListener("change", (e) => {
  clearInterval(rtTimer);
  if (e.target.checked) rtTimer = setInterval(loadRealtime, 15000);
});

// ---------------------------------------------------------------------------
// Trading control tab (交易管理) — suspend / resume / sell-all + audit log
// ---------------------------------------------------------------------------
const CTRL_LOG_COLS = ["ts", "action", "detail_json", "result"];
const CTRL_LOG_HEADERS = { ts: "时间", action: "操作", detail_json: "详情", result: "结果" };

async function loadControl() {
  if (!state.account) return;
  if (!hasNodeApi()) {
    $("#ctrl-state").textContent = "未配置节点";
    $("#ctrl-state").className = "";
    $("#ctrl-suspend").disabled = $("#ctrl-resume").disabled = true;
    nodeApiHint("#ctrl-log");
    return;
  }
  try {
    const st = await api("/api/control/state",
      { account: state.account.account_id, trader: state.account.trader_id });
    const paused = !!st.trading_paused;
    const b = $("#ctrl-state");
    b.textContent = paused ? "已暂停" : "运行中";
    b.className = paused ? "neg" : "pos";
    $("#ctrl-suspend").disabled = paused;
    $("#ctrl-resume").disabled = !paused;
    renderTable("#ctrl-log", st.recent_actions || [], {
      columns: CTRL_LOG_COLS, headers: CTRL_LOG_HEADERS,
    });
  } catch (e) {
    $("#ctrl-log").innerHTML = '<div class="empty">获取失败</div>';
    toast(e.message);
  }
}

async function controlPost(path, confirmMsg) {
  if (!hasNodeApi()) { toast("该账户未配置实盘节点 API"); return; }
  if (confirmMsg && !window.confirm(confirmMsg)) return;
  try {
    await api(path, { account: state.account.account_id, trader: state.account.trader_id }, { method: "POST" });
    loadControl();
  } catch (e) { toast(e.message); }
}
$("#ctrl-suspend").addEventListener("click", () => controlPost("/api/control/suspend"));
$("#ctrl-resume").addEventListener("click", () => controlPost("/api/control/resume"));
$("#ctrl-sell-all").addEventListener("click", () => controlPost("/api/control/sell_all", "确认卖出全部可卖持仓？此操作不可撤销。"));
$("#ctrl-refresh").addEventListener("click", loadControl);

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
fillMetricSelect($("#series-metric"), "asset");
initSources().catch((e) => toast("初始化失败: " + e.message));
