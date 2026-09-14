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

const NUMERIC_RE = /_rate|_bps|price|value|asset|cash|pnl|amount|qty|volume|weight|score|return|equity|commission|balance|sharpe|volatil/;
function isNumericCol(col) { return NUMERIC_RE.test(col); }

const RATE_COLS = new Set([
  "strat_daily_rate", "csi1000_daily_rate", "excess_daily_rate",
  "week_cum_strat_rate", "week_cum_csi1000_rate", "week_cum_excess_rate", "target_weight",
  "daily_volatility", "annual_volatility",
  // Signal-quality label returns / excess / decile spread are all rates (right % axis).
  "ls10", "top20_label_return", "top50_label_return",
  "top20_label_excess", "top50_label_excess", "benchmark_label_return",
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
const state = { source: null, accounts: [], account: null, sellEnabled: false };
// Sell gating is a backend decision (web/config.yaml: sell_enabled). It starts off so
// the UI never offers a sell action before /api/config has answered.
const SELL_OFF_HINT = "卖出功能未启用（web/config.yaml: sell_enabled）";
function setSellAllEnabled(on, hint = SELL_OFF_HINT) {
  const btn = $("#ctrl-sell-all");
  btn.disabled = !on;
  btn.title = on ? "" : hint;
}
function accountLabel(a) { return `${a.account_id} / ${a.trader_id}`; }
function accountKey(a) { return `${a.account_id}|${a.trader_id}`; }
function accountParams() {
  return { source: state.source, account: state.account.account_id, trader: state.account.trader_id };
}

// ---------------------------------------------------------------------------
// Bootstrapping
// ---------------------------------------------------------------------------
async function initSources() {
  const [{ sources }, cfg] = await Promise.all([api("/api/sources"), api("/api/config")]);
  state.sellEnabled = !!cfg.sell_enabled;
  setSellAllEnabled(state.sellEnabled);
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
  else if (tab === "sigqual") loadSignalQuality();
  else if (tab === "series") { if ($("#series-start").value && $("#series-end").value) $("#series-plot").click(); }
  else if (tab === "compare") { if (cmpSeries.length) plotCompare(); }
  else if (tab === "kline") { if ($("#kline-code").value.trim()) plotKline(); }
  else if (tab === "rline") { if ($("#rline-code").value.trim()) plotRline(); }
  else if (tab === "realtime") loadRealtime();
  else if (tab === "stratinfo") loadStratInfo();
  else if (tab === "control") { loadControl(); loadTargets(); }
}

// ---------------------------------------------------------------------------
// Sidebar nav + snapshot sub-tabs
// ---------------------------------------------------------------------------
// Select a sidebar nav item: set the visual active/open state and the right
// sub-panel. When `load` is true (a real user click) it also (re)fetches the
// tab's data and remembers the choice so a page refresh stays on this page.
// On restore (`load` false) the account-change flow does the fetching, so we
// only apply the visual state and skip the extra load.
const NAV_STORE_KEY = "dashboard.activeNav";

function saveActiveNav(btn) {
  try {
    localStorage.setItem(
      NAV_STORE_KEY,
      JSON.stringify({ tab: btn.dataset.tab, sub: btn.dataset.sub || null }),
    );
  } catch (e) { /* storage may be unavailable; persistence is best-effort */ }
}

function activateNavItem(btn, { load = true } = {}) {
  // A sidebar click ends any jump: drop the remembered origin so the chart
  // tabs' 返回 button doesn't point somewhere stale. (goBackFromChart clears it
  // before replaying the origin click, so this is a no-op in that path.)
  jumpOrigin = null;
  updateBackButtons();
  // Fold every group, then open only the one this item belongs to (if any).
  const group = btn.closest(".nav-group");
  $$(".nav-group").forEach((g) => g.classList.toggle("open", g === group));
  $$(".nav-item").forEach((b) => b.classList.remove("active"));
  $$(".tab-panel").forEach((p) => p.classList.remove("active"));
  btn.classList.add("active");
  // 离线报表 is a parent-only group with no panel of its own — clicking it defaults
  // to its first report (收益报表). Every other tab has a matching #tab-<id> panel.
  let tab = btn.dataset.tab;
  if (tab === "offline") tab = "report";
  const panel = $(`#tab-${tab}`);
  if (panel) panel.classList.add("active");
  // 实时信息 has 持仓 / 资产 sub-panels; a nav-sub click selects which one shows.
  // A click on the parent (no data-sub) defaults to 持仓.
  if (tab === "realtime") showRealtimeSub(btn.dataset.sub || "positions");
  if (tab === "stratinfo") showStratInfoSub(btn.dataset.sub || "signals");
  if (tab === "control") showControlSub(btn.dataset.sub || "control");
  saveActiveNav(btn);
  if (load) {
    if (tab === "report") loadReport();
    else if (tab === "sigqual") loadSignalQuality();
    else if (tab === "realtime") loadRealtime();
    else if (tab === "stratinfo") loadStratInfo();
    else if (tab === "control") { loadControl(); loadTargets(); }
  }
  setTimeout(resizeCharts, 0);
}

$$(".nav-item").forEach((btn) => {
  btn.addEventListener("click", () => activateNavItem(btn));
});

// Restore the last-visited page from a previous session so a refresh stays put
// instead of snapping back to 实时信息. Applies only the visual state; the
// account-change flow (refreshActiveTab) does the actual data fetch. Falls back
// to the default active nav if nothing valid is stored.
function restoreActiveNav() {
  let saved;
  try {
    saved = JSON.parse(localStorage.getItem(NAV_STORE_KEY) || "null");
  } catch (e) { saved = null; }
  if (!saved || !saved.tab) return;
  const btn = $$(".nav-item").find(
    (b) => b.dataset.tab === saved.tab && (b.dataset.sub || null) === (saved.sub || null),
  );
  if (btn) activateNavItem(btn, { load: false });
}

// Toggle between the 持仓 (#rt-positions) and 资产 (#rt-asset) sub-panels.
function showRealtimeSub(sub) {
  $$(".rt-sub-panel").forEach((p) => p.classList.remove("active"));
  const target = $(`#rt-${sub}`);
  if (target) target.classList.add("active");
}

// Toggle between the 控制 (#ctrl-control) and 当前目标 (#ctrl-targets) sub-panels.
function showControlSub(sub) {
  $$(".ctrl-sub-panel").forEach((p) => p.classList.remove("active"));
  const target = $(`#ctrl-${sub}`);
  if (target) target.classList.add("active");
}

$$(".subtab").forEach((btn) => {
  btn.addEventListener("click", () => {
    $$(".subtab").forEach((b) => b.classList.remove("active"));
    $$(".sub-panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    $(`#snap-${btn.dataset.sub}`).classList.add("active");
  });
});

// --- Back navigation for 个股K线 / 信号R线 -----------------------------------
// A name/code link jump switches tabs by toggling .active classes, losing the
// origin. Before jumping we snapshot the active nav-item (and, if it's the
// snapshot tab, its active sub-tab) so 返回 can re-open exactly where we came
// from by replaying the original clicks. Cleared once consumed.
let jumpOrigin = null;
function captureJumpOrigin() {
  const nav = document.querySelector(".nav-item.active");
  if (!nav) { jumpOrigin = null; return; }
  const activeSub = document.querySelector(".subtab.active");
  jumpOrigin = { nav, sub: activeSub ? activeSub.dataset.sub : null };
}
function updateBackButtons() {
  $("#kline-back").hidden = !jumpOrigin;
  $("#rline-back").hidden = !jumpOrigin;
}
function goBackFromChart() {
  const origin = jumpOrigin;
  jumpOrigin = null;
  updateBackButtons();
  if (!origin) return;
  origin.nav.click();               // restores group/panel/loader for the origin tab
  if (origin.sub) {                 // snapshot sub-tab (持仓/信号/…) isn't a nav-item
    const sub = document.querySelector(`.subtab[data-sub="${origin.sub}"]`);
    if (sub) sub.click();
  }
}
$("#kline-back").addEventListener("click", goBackFromChart);
$("#rline-back").addEventListener("click", goBackFromChart);

// Sidebar collapse / expand.
$("#sidebar-toggle").addEventListener("click", () => {
  const collapsed = document.body.classList.toggle("sidebar-collapsed");
  $("#sidebar-toggle").textContent = collapsed ? "»" : "«";
  setTimeout(resizeCharts, 0);
});

// ---------------------------------------------------------------------------
// Table renderer — with a leading 序号 column, per-column click-to-sort + a
// per-column filter row.
// ---------------------------------------------------------------------------
// The 序号 column is positional, not data: it numbers the *visible* rows, so it
// renumbers after every sort/filter and carries no value into the footer row.
const IDX_HEAD = '<th class="idx-col">序号</th>';
const IDX_FOOT = '<td class="idx-col"></td>';

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
    // Code column → 个股K线 (.klink); name column → 信号R线 (.rlink). The 信号 table keys
    // its name column "name", the others "stock_name"; either way the code comes from
    // row.stock_code (present on every linkable row via attach_names).
    if (allowLink && linkStock && row && row.stock_code) {
      if (c === "stock_code") {
        disp = `<a class="klink" data-code="${row.stock_code}">${disp || row.stock_code}</a>`;
      } else if (c === "stock_name" || c === "name") {
        disp = `<a class="rlink" data-code="${row.stock_code}">${disp || row.stock_code}</a>`;
      }
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
    `<table><thead><tr class="head-row">${IDX_HEAD}${headCells}</tr></thead>` +
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
    table.querySelector("tbody").innerHTML = view.map((row, i) => {
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
      return `<tr class="${rowCls}"><td class="idx-col">${i + 1}</td>` +
        `${cols.map((c) => cellHtml(c, row[c], row, true)).join("")}</tr>`;
    }).join("");
    // 4. footer (recomputed from the visible rows when a footerFn is given)
    const footerRow = opts.footerFn ? opts.footerFn(view) : opts.footerRow;
    const fcls = opts.footerRowClass || "sum-row";
    table.querySelector("tfoot").innerHTML = footerRow
      ? `<tr class="${fcls}">${IDX_FOOT}${cols.map((c) => cellHtml(c, footerRow[c], footerRow, false)).join("")}</tr>`
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
    el.querySelectorAll("a.rlink").forEach((a) => a.addEventListener("click", () => openRline(a.dataset.code)));
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
  ["#snap-asset", "#snap-positions", "#snap-target", "#snap-orders", "#snap-trades", "#snap-signals"]
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
  // 信号: the historical ranked signal cross-section for the selected date, read from the
  // ClickHouse warehouse (needs a live node only to resolve the table name). Loaded
  // separately so a missing node or fetch error never blocks the other snapshot panels.
  if (!hasNodeApi()) { nodeApiHint("#snap-signals"); return; }
  try {
    const sig = await api("/api/snapshot_signals",
      { account: state.account.account_id, trader: state.account.trader_id, date });
    renderTable("#snap-signals", sig.signals || [], {
      columns: SNAP_SIGNAL_COLS, headers: SNAP_SIGNAL_HEADERS,
      intCols: new Set(["rank"]),
      rateCols: new Set(["pred_return_live"]),
      signCols: new Set(["pred_return_live"]),
      linkStock: true,
    });
  } catch (e) { $("#snap-signals").innerHTML = '<div class="empty">获取失败</div>'; }
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
  daily_volatility: "日波动率",
  annual_volatility: "年化波动率",
  daily_sharpe: "日夏普",
  annual_sharpe: "年化夏普",
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
  "daily_sharpe", "annual_sharpe",
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
// Signal-quality report tab (信号质量)
// ---------------------------------------------------------------------------
// Per-day metric columns (must match SIGNAL_QUALITY_COLUMNS in the backend).
const SIGQUAL_METRICS = [
  "rankic", "ic", "ls10",
  "top20_label_return", "top50_label_return",
  "top20_label_excess", "top50_label_excess",
  "benchmark_label_return", "sample_count",
];
const SQ_HEADERS = {
  trade_date: "日期",
  rankic: "RankIC",
  ic: "IC",
  ls10: "LS10",
  top20_label_return: "Top20收益",
  top50_label_return: "Top50收益",
  top20_label_excess: "Top20超额",
  top50_label_excess: "Top50超额",
  benchmark_label_return: "基准收益",
  sample_count: "样本数",
  label_source: "标签来源",
};
// Trailing-edge fallback flag -> Chinese. normal = exact open-to-open forward
// return; t1_intraday = 复权 T+1 open -> 现价 (buy at T+1 open, hold to now);
// realtime_intraday = live (现价/开).
const SQ_LABEL_SOURCE = {
  normal: "正常",
  t1_intraday: "T+1持有至今",
  realtime_intraday: "实时日内",
};
// ls10 / label returns / excess are on the % axis (RATE_COLS). rankic/ic are plain
// signed 4-decimal numerics; sample_count is an int.
const SQ_SIGN_COLS = new Set([
  "rankic", "ic", "ls10",
  "top20_label_return", "top50_label_return",
  "top20_label_excess", "top50_label_excess", "benchmark_label_return",
]);
const SQ_INT_COLS = new Set(["sample_count"]);

// Small stats helpers matching the reference aggregation (pandas mean / sample std
// ddof=1 / share > 0). NaN-safe: they operate on the finite numbers only.
function _finite(arr) { return arr.filter((x) => typeof x === "number" && isFinite(x)); }
function _mean(arr) { const a = _finite(arr); return a.length ? a.reduce((s, x) => s + x, 0) / a.length : NaN; }
function _stdSample(arr) {
  const a = _finite(arr);
  if (a.length < 2) return NaN;
  const m = a.reduce((s, x) => s + x, 0) / a.length;
  const v = a.reduce((s, x) => s + (x - m) * (x - m), 0) / (a.length - 1);
  return Math.sqrt(v);
}
function _positiveRatio(arr) { const a = _finite(arr); return a.length ? a.filter((x) => x > 0).length / a.length : NaN; }
function _fmtNum(v, d = 4) { return (typeof v === "number" && isFinite(v)) ? v.toFixed(d) : "—"; }
function _fmtPct(v) { return (typeof v === "number" && isFinite(v)) ? (v * 100).toFixed(2) + "%" : "—"; }

// Footer for 信号质量: period-level scalars computed over the visible per-day rows,
// matching the reference's sub.ic.mean()/sub.ic.std() aggregation. RankICIR/ICIR go
// under their base column; positive ratios under the topN columns; TopN means are the
// straight column means (rate-formatted by the table).
function buildSignalQualitySummary(rows) {
  if (!rows || rows.length === 0) return null;
  const col = (c) => rows.map((r) => r[c]);
  const rankicMean = _mean(col("rankic")), rankicStd = _stdSample(col("rankic"));
  const icMean = _mean(col("ic")), icStd = _stdSample(col("ic"));
  const rankicIR = (isFinite(rankicStd) && rankicStd !== 0) ? rankicMean / rankicStd : NaN;
  const icIR = (isFinite(icStd) && icStd !== 0) ? icMean / icStd : NaN;
  const out = { trade_date: "合计/IR" };
  // RankIC column: mean + RankICIR + positive ratio.
  out.rankic = `均值 ${_fmtNum(rankicMean)}｜IR ${_fmtNum(rankicIR, 3)}｜胜率 ${_fmtPct(_positiveRatio(col("rankic")))}`;
  out.ic = `均值 ${_fmtNum(icMean)}｜IR ${_fmtNum(icIR, 3)}｜胜率 ${_fmtPct(_positiveRatio(col("ic")))}`;
  out.ls10 = `均值 ${_fmtPct(_mean(col("ls10")))}`;
  // TopN columns: mean return + positive ratio (share of days with return > 0).
  out.top20_label_return = `均值 ${_fmtPct(_mean(col("top20_label_return")))}｜胜率 ${_fmtPct(_positiveRatio(col("top20_label_return")))}`;
  out.top50_label_return = `均值 ${_fmtPct(_mean(col("top50_label_return")))}｜胜率 ${_fmtPct(_positiveRatio(col("top50_label_return")))}`;
  out.top20_label_excess = `均值 ${_fmtPct(_mean(col("top20_label_excess")))}`;
  out.top50_label_excess = `均值 ${_fmtPct(_mean(col("top50_label_excess")))}`;
  out.benchmark_label_return = `均值 ${_fmtPct(_mean(col("benchmark_label_return")))}`;
  out.sample_count = "";
  out.label_source = "";
  return out;
}

async function loadSignalQuality() {
  if (!state.account) return;
  if (!hasNodeApi()) { nodeApiHint("#sq-table"); return; }
  const start = $("#sq-start").value || monthsBefore(null, 1);
  const end = $("#sq-end").value || todayISO();
  const holding = parseInt($("#sq-holding").value, 10) || 3;
  if (!$("#sq-start").value) $("#sq-start").value = start;
  if (!$("#sq-end").value) $("#sq-end").value = end;
  try {
    const res = await api("/api/signal_quality", { ...accountParams(), start, end, holding_days: holding });
    const { columns, rows } = res;
    const hint = $("#sq-hint");
    if (hint) {
      hint.textContent = `预测表 ${res.predictions_table || "?"}｜窗口 ${res.holding_days} 日｜`
        + "标签=复权开盘价 t+1→t+(N+1)，基准=中证全指；"
        + "近端信号无完整未来窗口时，用 T+1 持有至今(复权开→现价)或实时(现价/开)近似，见「标签来源」列";
    }
    renderTable("#sq-table", rows, {
      columns,
      headers: SQ_HEADERS,
      signCols: SQ_SIGN_COLS,
      intCols: SQ_INT_COLS,
      footerFn: buildSignalQualitySummary,
      // Map the raw label_source flag to Chinese; leave every other cell default.
      cellFn: (c, v) => (c === "label_source" ? `<td class="text">${SQ_LABEL_SOURCE[v] || v || ""}</td>` : undefined),
      // Amber-tint any approximate (non-normal) row so it's never mistaken for exact.
      rowClass: (row) => (row.label_source && row.label_source !== "normal" ? "approx-row" : ""),
    });
  } catch (e) { toast(e.message); $("#sq-table").innerHTML = '<div class="empty">无数据</div>'; }
}
$("#sq-refresh").addEventListener("click", loadSignalQuality);
$("#sq-holding").addEventListener("change", loadSignalQuality);

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
// A container that is rebuilt from innerHTML detaches its chart canvas; dispose the
// cached instance first so ECharts never keeps a dead DOM node.
function disposeChart(id) {
  if (charts[id]) {
    charts[id].dispose();
    delete charts[id];
  }
}
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
  "daily_volatility", "annual_volatility", "daily_sharpe", "annual_sharpe",
  "return_amount", "week_cum_return_amount", "before_market_value", "after_market_value",
  "buy_slippage_bps", "sell_slippage_bps", "total_slippage_bps",
];
const metricList = (dataset) =>
  dataset === "returns" ? RETURN_METRICS
  : dataset === "sigqual" ? SIGQUAL_METRICS
  : ASSET_METRICS;
// Single-select picker, used by the 对比 series builder.
function fillMetricSelect(sel, dataset) {
  sel.innerHTML = metricList(dataset).map((m) => `<option>${m}</option>`).join("");
}

// ---- 指标 multi-select (checkbox dropdown) ----
// Several metrics on one chart is the normal case here (strategy vs benchmark vs
// excess rate, or equity against its pnl components), so the 随时间 picker is a
// checkbox panel rather than a single <select>.
function renderMetricPanel(metrics) {
  $("#series-metric-panel").innerHTML =
    `<div class="ms-list">${metrics.map((m) =>
      `<label class="ms-opt"><input type="checkbox" value="${m}" /><span>${m}</span></label>`).join("")}</div>
     <div class="ms-actions">
       <button type="button" data-act="all">全选</button>
       <button type="button" data-act="none">清空</button>
     </div>`;
}
function selectedMetrics() {
  return $$("#series-metric-panel input:checked").map((i) => i.value);
}
function updateMetricLabel() {
  const sel = selectedMetrics();
  $("#series-metric-toggle").textContent =
    sel.length === 0 ? "选择指标" : sel.length === 1 ? sel[0] : `${sel.length} 个指标`;
}
function setAllMetrics(checked) {
  $$("#series-metric-panel input").forEach((i) => { i.checked = checked; });
  updateMetricLabel();
}
// Each dataset has its own column set, so switching resets the list + selection.
function resetMetricSelection(metrics) {
  renderMetricPanel(metrics);
  const first = $("#series-metric-panel input");
  if (first) first.checked = true;
  updateMetricLabel();
}
// Selecting/deselecting an indicator (or switching 数据类型, which resets the list)
// redraws right away — 绘制 stays only as an explicit re-fetch.
$("#series-dataset").addEventListener("change", (e) => {
  resetMetricSelection(metricList(e.target.value));
  plotSeries(true);
});
$("#series-metric-toggle").addEventListener("click", (e) => {
  e.stopPropagation();
  $("#series-metric-ms").classList.toggle("open");
});
$("#series-metric-panel").addEventListener("click", (e) => {
  const act = e.target.dataset.act;
  if (act) { e.preventDefault(); setAllMetrics(act === "all"); plotSeries(true); }
});
$("#series-metric-panel").addEventListener("change", (e) => {
  updateMetricLabel();
  if (e.target.type === "checkbox") plotSeries(true);
});
document.addEventListener("click", (e) => {
  const owner = e.target.closest && e.target.closest("#series-metric-ms");
  if (!owner) $("#series-metric-ms").classList.remove("open");
});

// Rate columns (~0.0x) and magnitude columns (~1e6) cannot share one linear axis
// without one of them going flat, so when both kinds are selected the rates move
// to a right-hand % axis. Returns metric -> yAxisIndex.
function applyMetricAxes(opt, metrics) {
  const pctFmt = (v) => (v * 100).toFixed(1) + "%";
  const hasRate = metrics.some((m) => RATE_COLS.has(m));
  const hasMag = metrics.some((m) => !RATE_COLS.has(m));
  if (!hasRate || !hasMag) {
    if (hasRate) opt.yAxis.axisLabel.formatter = pctFmt;
    return () => 0;
  }
  opt.grid.right = 78;  // room for the right-hand axis labels
  opt.yAxis = [
    { type: "value", axisLabel: { color: "#8a94a8" }, splitLine: { lineStyle: { color: "#2a3346" } } },
    { type: "value", position: "right", axisLabel: { color: "#8a94a8", formatter: pctFmt }, splitLine: { show: false } },
  ];
  return (m) => (RATE_COLS.has(m) ? 1 : 0);
}
const tipNum = (v) => (v === null || v === undefined ? "-"
  : Number(v).toLocaleString(undefined, { maximumFractionDigits: 4 }));
const tipPct = (v) => (v === null || v === undefined ? "-" : (v * 100).toFixed(4) + "%");

async function fetchSeriesData(dataset, base, start, end) {
  if (dataset === "returns") return (await api("/api/returns", { ...base, start, end })).rows;
  // Signal quality: chart uses the default 3-day forward window (the report tab lets
  // you switch it). Rows are keyed on trade_date, same as the other datasets.
  if (dataset === "sigqual") return (await api("/api/signal_quality", { ...base, start, end, holding_days: 3 })).rows;
  return (await api("/api/asset", { ...base, start, end, snapshot_type: "after_trading" })).rows;
}

// Default window: end = today, start = one month ago (only when blank).
function defaultSeriesRange() {
  if (!$("#series-end").value) $("#series-end").value = todayISO();
  if (!$("#series-start").value) $("#series-start").value = monthsBefore(null, 1);
}

// auto=true is the change-driven path: a half-filled form is skipped quietly
// instead of toasting on every pick. Draw requests are sequenced because several
// can be in flight once a control redraws on change — only the newest may paint.
let seriesDraw = 0;
async function plotSeries(auto = false) {
  const draw = ++seriesDraw;
  const dataset = $("#series-dataset").value;
  const metrics = selectedMetrics();
  const start = $("#series-start").value, end = $("#series-end").value;
  if (!start || !end) { if (!auto) toast("请选择起止日期"); return; }
  if (!metrics.length) { if (!auto) toast("请至少选择一个指标"); return; }
  try {
    const rows = await fetchSeriesData(dataset, accountParams(), start, end);
    if (draw !== seriesDraw) return;
    const opt = baseLineOption(`${accountLabel(state.account)} · ${metrics.join(" / ")}`);
    opt.xAxis.data = rows.map((r) => r.trade_date);
    const axisFor = applyMetricAxes(opt, metrics);
    if (metrics.length > 1) {
      // Scrollable single-row legend keeps the header from crowding the plot.
      opt.legend = { type: "scroll", top: 28, left: 10, right: 10, textStyle: { color: "#8a94a8" } };
    }
    opt.series = metrics.map((m) => ({
      name: m, type: "line", showSymbol: false, connectNulls: true,
      yAxisIndex: axisFor(m),
      tooltip: { valueFormatter: RATE_COLS.has(m) ? tipPct : tipNum },
      data: rows.map((r) => r[m]),
    }));
    getChart("series-chart").setOption(opt, true);
  } catch (e) { if (draw === seriesDraw) toast(e.message); }
}
$("#series-plot").addEventListener("click", () => plotSeries());
["#series-start", "#series-end"].forEach((sel) => {
  $(sel).addEventListener("change", () => plotSeries(true));
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
  captureJumpOrigin();
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
  updateBackButtons();
  setTimeout(plotKline, 0);
}
// Change-driven redraws follow the same rule as plotSeries: quiet on incomplete
// input, and only the newest in-flight request may paint.
let klineDraw = 0;
async function plotKline(auto = false) {
  const draw = ++klineDraw;
  const code = $("#kline-code").value.trim();
  const start = $("#kline-start").value, end = $("#kline-end").value;
  const withMarks = $("#kline-marks").checked;
  if (!code || !start || !end) { if (!auto) toast("请填写代码与起止日期"); return; }
  try {
    let bars, trades = [], name = "";
    if (withMarks && state.account) {
      const d = await api("/api/kline_with_trades", { ...accountParams(), stock_code: code, start, end });
      bars = d.bars; trades = d.trades; name = d.stock_name;
    } else {
      const d = await api("/api/kline", { stock_code: code, start, end, source: state.source });
      bars = d.rows; name = d.stock_name;
    }
    if (draw !== klineDraw) return;
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
  } catch (e) { if (draw === klineDraw) toast(e.message); }
}
$("#kline-plot").addEventListener("click", () => plotKline());
// Same as the 随时间 tab: a date or 显示买卖点 change redraws without 绘制.
["#kline-start", "#kline-end"].forEach((sel) => {
  $(sel).addEventListener("change", () => plotKline(true));
});
$("#kline-marks").addEventListener("change", () => plotKline(true));

// ---------------------------------------------------------------------------
// 信号R线 tab — per-stock daily signal rank/score with buy/sell markers
// ---------------------------------------------------------------------------
// Jumping from a table name link: always show the latest month (end = today,
// start = one month ago), overriding whatever was in the inputs before.
function openRline(stockCode) {
  captureJumpOrigin();
  $("#rline-code").value = stockCode;
  $("#rline-end").value = iso(new Date());
  $("#rline-start").value = monthsBefore(null, 1);
  $$(".nav-group").forEach((g) => g.classList.remove("open"));
  $$(".nav-item").forEach((b) => b.classList.remove("active"));
  $$(".tab-panel").forEach((p) => p.classList.remove("active"));
  document.querySelector('.nav-item[data-tab="rline"]').classList.add("active");
  $("#tab-rline").classList.add("active");
  updateBackButtons();
  setTimeout(plotRline, 0);
}
let rlineDraw = 0;
async function plotRline(auto = false) {
  const draw = ++rlineDraw;
  const code = $("#rline-code").value.trim();
  const start = $("#rline-start").value, end = $("#rline-end").value;
  const withMarks = $("#rline-marks").checked;
  if (!code || !start || !end) { if (!auto) toast("请填写代码与起止日期"); return; }
  if (!state.account) { if (!auto) toast("请选择账户"); return; }
  try {
    const d = await api("/api/signal_series", { ...accountParams(), stock_code: code, start, end });
    if (draw !== rlineDraw) return;
    const series = d.series || [], trades = d.trades || [], name = d.stock_name || "";
    if (series.length === 0) { toast("无该股票信号数据"); return; }

    const dates = series.map((r) => String(r.date).slice(0, 10));
    const rank = series.map((r) => (r.rank == null ? null : Number(r.rank)));
    const score = series.map((r) => (r.score == null ? null : Number(r.score)));
    const rankByDate = {};
    series.forEach((r, i) => { rankByDate[dates[i]] = rank[i]; });

    // Aggregate fills per day (for the tooltip), like 个股K线.
    const dayFills = {};
    trades.forEach((t) => {
      const dd = String(t.trade_date).slice(0, 10);
      const px = Number(t.price) || 0, qty = Number(t.quantity) || 0;
      const amt = Number(t.amount) != null && !isNaN(Number(t.amount)) && Number(t.amount) !== 0
        ? Number(t.amount) : px * qty;
      const side = String(t.side).toLowerCase() === "buy" ? "buy" : "sell";
      const day = dayFills[dd] || (dayFills[dd] = {
        buy: { qty: 0, pxQty: 0, amt: 0 }, sell: { qty: 0, pxQty: 0, amt: 0 },
      });
      day[side].qty += qty; day[side].pxQty += px * qty; day[side].amt += amt;
    });

    // Buy/sell markers anchored on the rank line (blue up-arrow / orange down-pin),
    // mirroring 个股K线. The rank axis is inverted, so "up" (toward rank 1) is a
    // positive y-offset in pixels; "down" is negative.
    const SYM = 16, GAP = 6;
    const markData = trades.map((t) => {
      const dd = String(t.trade_date).slice(0, 10);
      const isBuy = String(t.side).toLowerCase() === "buy";
      const anchor = rankByDate[dd];
      if (anchor == null) return null;
      const yOffset = isBuy ? (SYM / 2 + GAP) : -(SYM / 2 + GAP);
      return {
        name: isBuy ? "买" : "卖", coord: [dd, anchor],
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
    }).filter(Boolean);

    const title = name ? `${code} ${name} 信号R线` : `${code} 信号R线`;
    const tipHtml = (ps) => {
      const i = ps[0].dataIndex;
      const rows = [`<b>${ps[0].axisValue}</b>`];
      if (rank[i] != null) rows.push(`排名 ${fmt(rank[i])}`);
      if (score[i] != null) rows.push(`分数 ${fmt(score[i])}`);
      const day = dayFills[dates[i]];
      if (day) {
        const parts = [];
        if (day.buy.qty) parts.push(`<span style="color:#4c8dff">买 均价 ${fmt(day.buy.pxQty / day.buy.qty)}　量 ${fmt(day.buy.qty)}　额 ${fmt(day.buy.amt)}</span>`);
        if (day.sell.qty) parts.push(`<span style="color:#ffa726">卖 均价 ${fmt(day.sell.pxQty / day.sell.qty)}　量 ${fmt(day.sell.qty)}　额 ${fmt(day.sell.amt)}</span>`);
        if (parts.length) rows.push('<div style="border-top:1px dashed #4c5468;margin:4px 0 2px"></div>' + parts.join("<br/>"));
      }
      return rows.join("<br/>");
    };

    const opt = {
      backgroundColor: "transparent",
      title: { text: title, left: 10, textStyle: { color: "#d7dce6", fontSize: 14 } },
      tooltip: { trigger: "axis", axisPointer: { type: "cross" }, formatter: tipHtml, confine: true },
      legend: { data: ["排名", "分数"], textStyle: { color: "#8a94a8" }, left: 10, top: 26 },
      grid: { left: 66, right: 66, top: 74, bottom: 60 },
      xAxis: { type: "category", data: dates, axisLabel: { color: "#8a94a8" } },
      yAxis: [
        // Rank: 1 is best → put it on top (inverse), start at 1.
        { name: "排名", inverse: true, min: 1, axisLabel: { color: "#8a94a8" },
          splitLine: { lineStyle: { color: "#2a3346" } } },
        // Score: right axis, auto-scaled.
        { name: "分数", scale: true, position: "right", axisLabel: { color: "#8a94a8" },
          splitLine: { show: false } },
      ],
      dataZoom: [
        { type: "inside", xAxisIndex: 0 },
        { type: "slider", xAxisIndex: 0, height: 18, bottom: 8 },
      ],
      series: [
        {
          name: "排名", type: "line", data: rank, yAxisIndex: 0, connectNulls: true,
          showSymbol: false, lineStyle: { color: "#4c8dff", width: 2 }, itemStyle: { color: "#4c8dff" },
          markPoint: withMarks ? { data: markData, tooltip: { formatter: (p) => p.data.value } } : undefined,
        },
        {
          name: "分数", type: "line", data: score, yAxisIndex: 1, connectNulls: true,
          showSymbol: false, lineStyle: { color: "#ffa726", width: 2 }, itemStyle: { color: "#ffa726" },
        },
      ],
    };
    getChart("rline-chart").setOption(opt, true);
  } catch (e) { if (draw === rlineDraw) toast(e.message); }
}
$("#rline-plot").addEventListener("click", () => plotRline());
["#rline-start", "#rline-end"].forEach((sel) => {
  $(sel).addEventListener("change", () => plotRline(true));
});
$("#rline-marks").addEventListener("change", () => plotRline(true));

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
  // sell_enabled=false → grayed out, and a disabled button never fires the click.
  const gate = state.sellEnabled ? "" : `disabled title="${SELL_OFF_HINT}"`;
  return `<td class="text"><button class="rt-sell" ${gate} data-code="${row.stock_code}" data-name="${row.name || ""}" data-qty="${row.can_use_volume}">卖出</button></td>`;
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

// 资产 sub-panel — account-level assets from the same /realtime/positions payload
// (the node's control server returns `asset` alongside `positions`). Rendered as a
// labeled key/value card mirroring the broker 资金 screen, plus a 资产构成 pie of
// 持仓市值 / 可用资金 / 冻结资金 — the three parts that make up 总资产.
const RT_ASSET_ROWS = [
  ["total_asset", "总资产"],
  ["market_value", "持仓市值"],
  ["cash", "资金余额"],
  ["available_cash", "可用资金"],
  ["frozen_cash", "冻结资金"],
];
const RT_ASSET_PIE_ID = "rt-asset-pie";
// The pie's three slices, by asset key — labels come from RT_ASSET_ROWS so the two
// views of the same numbers can't drift apart.
const RT_ASSET_PIE_KEYS = ["market_value", "available_cash", "frozen_cash"];

function renderRealtimeAssetPie(asset) {
  const labels = new Map(RT_ASSET_ROWS);
  const el = $("#" + RT_ASSET_PIE_ID);
  const data = RT_ASSET_PIE_KEYS
    .map((k) => ({ name: labels.get(k), value: Number(asset[k]) || 0 }))
    .filter((d) => d.value > 0);
  if (!data.length) { el.innerHTML = '<div class="empty">无资产构成</div>'; return; }
  getChart(RT_ASSET_PIE_ID).setOption({
    backgroundColor: "transparent",
    title: { text: "资产构成", left: "center", textStyle: { color: "#d7dce6", fontSize: 14 } },
    tooltip: { trigger: "item", formatter: (p) => `${p.name}&nbsp;&nbsp;${fmt(p.value)}（${p.percent}%）` },
    legend: { bottom: 0, textStyle: { color: "#8a94a8" } },
    series: [{
      type: "pie",
      radius: ["40%", "64%"],
      center: ["50%", "52%"],
      label: { color: "#d7dce6", formatter: "{b}\n{d}%" },
      labelLine: { length: 8, length2: 10 },
      itemStyle: { borderColor: "#171d2b", borderWidth: 2 },
      data,
    }],
  }, true);
}

function renderRealtimeAsset(asset, emptyText = "无资产数据") {
  const el = $("#rt-asset");
  disposeChart(RT_ASSET_PIE_ID);
  const rows = asset
    ? RT_ASSET_ROWS
      .filter(([k]) => asset[k] !== null && asset[k] !== undefined)
      .map(([k, label]) => `<tr><th class="text">${label}</th><td>${fmt(asset[k])}</td></tr>`)
      .join("")
    : "";
  if (!rows) { el.innerHTML = `<div class="empty">${emptyText}</div>`; return; }
  const acct = asset.account ? `<caption class="text">资金账号 ${asset.account}</caption>` : "";
  el.innerHTML =
    `<div class="asset-split"><table class="kv-asset">${acct}<tbody>${rows}</tbody></table>` +
    `<div id="${RT_ASSET_PIE_ID}" class="asset-pie"></div></div>`;
  renderRealtimeAssetPie(asset);
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
  if (!hasNodeApi()) {
    nodeApiHint("#rt-positions");
    renderRealtimeAsset(null, "该账户未配置实盘节点 API（node_api）");
    $("#rt-updated").textContent = "";
    return;
  }
  try {
    const { positions, asset } = await api("/api/realtime/positions",
      { account: state.account.account_id, trader: state.account.trader_id });
    renderRealtime(positions, asset);
    renderRealtimeAsset(asset);
    $("#rt-updated").textContent = "更新于 " + new Date().toLocaleTimeString();
  } catch (e) {
    $("#rt-positions").innerHTML = '<div class="empty">获取失败</div>';
    renderRealtimeAsset(null, "获取失败");
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
// Strategy info tab (策略信息) — 信号 (top-50 origin signals) + 策略 (key config).
// Both read the live node's in-memory strategy state via the control API, so
// the views reflect exactly what this node is trading on. Data-driven so new
// node-side fields render without frontend changes (extendable by design).
// ---------------------------------------------------------------------------
const SI_SIGNAL_COLS = ["rank", "stock_code", "name", "score", "pred_return_live", "day_change", "day_change_open", "held"];
const SI_SIGNAL_HEADERS = {
  rank: "排名", stock_code: "证券代码", name: "证券名称",
  score: "评分", pred_return_live: "预测收益", day_change: "当日涨幅",
  day_change_open: "当日涨幅(开)", held: "已持仓",
};
// 按日快照 → 信号: historical warehouse cross-section (no live-runtime 当日涨幅/已持仓 columns).
const SNAP_SIGNAL_COLS = ["rank", "stock_code", "stock_name", "score", "pred_return_live"];
const SNAP_SIGNAL_HEADERS = {
  rank: "排名", stock_code: "证券代码", stock_name: "证券名称",
  score: "评分", pred_return_live: "预测收益",
};
const SI_INFO_LABELS = {
  risk_model_id: "风险模型 ID", alpha_model_id: "Alpha 模型 ID",
  risk_manager_mode: "风控模式", predictions_table: "预测表",
  max_positions: "最大持仓数",
};

function showStratInfoSub(sub) {
  $$(".si-sub-panel").forEach((p) => p.classList.remove("active"));
  const target = $(`#si-${sub}`);
  if (target) target.classList.add("active");
  // The signal summary belongs to the 信号 view only.
  $("#si-summary").style.display = sub === "signals" ? "" : "none";
}

// --- live signal-quality helpers (client-side, mirror the reference IC/RankIC) ---
// Pearson correlation over paired finite numbers.
function _pearson(xs, ys) {
  const n = xs.length;
  if (n < 2) return NaN;
  const mx = xs.reduce((s, v) => s + v, 0) / n;
  const my = ys.reduce((s, v) => s + v, 0) / n;
  let sxy = 0, sxx = 0, syy = 0;
  for (let i = 0; i < n; i++) {
    const dx = xs[i] - mx, dy = ys[i] - my;
    sxy += dx * dy; sxx += dx * dx; syy += dy * dy;
  }
  const denom = Math.sqrt(sxx * syy);
  return denom === 0 ? NaN : sxy / denom;
}
// Average-rank transform (ties share their mean rank) for Spearman.
function _avgRanks(vals) {
  const idx = vals.map((v, i) => [v, i]).sort((a, b) => a[0] - b[0]);
  const ranks = new Array(vals.length);
  let i = 0;
  while (i < idx.length) {
    let j = i;
    while (j + 1 < idx.length && idx[j + 1][0] === idx[i][0]) j++;
    const avg = (i + j) / 2 + 1; // 1-based average rank across the tie block
    for (let k = i; k <= j; k++) ranks[idx[k][1]] = avg;
    i = j + 1;
  }
  return ranks;
}
// safe_corr guard from the reference: need >=3 paired points and >=2 unique values on
// each side, else NaN. method: "pearson" | "spearman".
function _safeCorr(xs, ys, method) {
  if (xs.length < 3) return NaN;
  const uniq = (a) => new Set(a).size;
  if (uniq(xs) < 2 || uniq(ys) < 2) return NaN;
  if (method === "spearman") return _pearson(_avgRanks(xs), _avgRanks(ys));
  return _pearson(xs, ys);
}

// A UI-only rollup of the loaded signal rows — no backend involvement. Shows the
// up/down/flat/held counts plus live signal-quality chips computed from the visible
// rows: RankIC/IC of score vs the realized label, and Top50 收益/胜率. These mirror
// the reference IC/RankIC/TopN definitions. Two label proxies are shown side by side:
//   · 当日涨幅     (close-to-close off 昨收) — the original proxy
//   · 当日涨幅(开)  (last_price/open − 1)    — the open-anchored intraday proxy, closest
//                                             to the offline open-to-open forward label.
function _signalQualityChips(rows, labelKey, suffix) {
  // Pairs with a numeric score AND a numeric label feed the IC/RankIC.
  const valued = rows.filter((r) => typeof r.score === "number" && typeof r[labelKey] === "number");
  const scores = valued.map((r) => r.score);
  const labels = valued.map((r) => r[labelKey]);
  const rankic = _safeCorr(scores, labels, "spearman");
  const ic = _safeCorr(scores, labels, "pearson");
  const corrChip = (label, v) => {
    if (!isFinite(v)) return `<span class="si-chip">${label} <b>样本不足</b></span>`;
    const cls = v > 0 ? "pos" : v < 0 ? "neg" : "";
    return `<span class="si-chip ${cls}">${label} <b>${v.toFixed(4)}</b></span>`;
  };
  // Top50 by score (rows already arrive rank-ordered, but sort defensively). Uses all
  // valued rows when fewer than 50 are present, noting the count.
  const topSorted = valued.slice().sort((a, b) => b.score - a.score);
  const topN = topSorted.slice(0, 50);
  const n = topN.length;
  const topMean = n ? topN.reduce((s, r) => s + r[labelKey], 0) / n : NaN;
  const topWin = n ? topN.filter((r) => r[labelKey] > 0).length / n : NaN;
  const topLabel = (n < 50 ? `Top${n}` : "Top50") + suffix;
  const topRetChip = isFinite(topMean)
    ? `<span class="si-chip ${topMean > 0 ? "pos" : topMean < 0 ? "neg" : ""}">${topLabel}收益 <b>${(topMean * 100).toFixed(2)}%</b></span>`
    : "";
  const topWinChip = isFinite(topWin)
    ? `<span class="si-chip">${topLabel}胜率 <b>${(topWin * 100).toFixed(1)}%</b></span>`
    : "";
  return corrChip("RankIC" + suffix, rankic) + corrChip("IC" + suffix, ic) + topRetChip + topWinChip;
}

function renderSignalSummary(signals) {
  const el = $("#si-summary");
  const rows = signals || [];
  if (!rows.length) { el.innerHTML = ""; return; }
  let up = 0, down = 0, flat = 0, unknown = 0, held = 0;
  for (const r of rows) {
    if (r.held) held += 1;
    const c = r.day_change;
    if (typeof c !== "number") unknown += 1;
    else if (c > 0) up += 1;
    else if (c < 0) down += 1;
    else flat += 1;
  }
  const total = rows.length;
  const rate = (n) => ` <span class="si-rate">${(n / total * 100).toFixed(1)}%</span>`;
  const chip = (label, value, cls = "", withRate = true) =>
    `<span class="si-chip ${cls}">${label} <b>${value}</b>${withRate ? rate(value) : ""}</span>`;

  const countRow =
    chip("共", total, "", false) +
    chip("上涨", up, "pos") +
    chip("下跌", down, "neg") +
    chip("平", flat) +
    (unknown ? chip("无价", unknown) : "") +
    chip("已持仓", held, "held");

  // Three stacked rows: counts, then the two label-proxy metric groups.
  el.innerHTML =
    `<div class="si-row">${countRow}</div>` +
    `<div class="si-row">${_signalQualityChips(rows, "day_change", "(实时)")}</div>` +
    `<div class="si-row">${_signalQualityChips(rows, "day_change_open", "(开盘)")}</div>`;
}

async function loadStratInfo() {
  if (!state.account) return;
  if (!hasNodeApi()) {
    nodeApiHint("#si-signals");
    nodeApiHint("#si-strategy");
    $("#si-meta").textContent = "";
    return;
  }
  const params = { account: state.account.account_id, trader: state.account.trader_id };
  try {
    const sig = await api("/api/strategy/signals", params);
    $("#si-meta").textContent =
      `预测表：${sig.predictions_table || "—"} · 信号日期：${sig.signal_date || "—"} · 共 ${sig.count || 0} 条`;
    renderSignalSummary(sig.signals || []);
    renderTable("#si-signals", sig.signals || [], {
      columns: SI_SIGNAL_COLS, headers: SI_SIGNAL_HEADERS,
      intCols: new Set(["rank"]),
      rateCols: new Set(["pred_return_live", "day_change", "day_change_open"]),
      signCols: new Set(["pred_return_live", "day_change", "day_change_open"]),
      linkStock: true,
      rowClass: (row) => (row.held ? "held-row" : ""),
      cellFn: (c, v) => (c === "held" ? `<td>${v ? "✓" : ""}</td>` : undefined),
    });
  } catch (e) {
    $("#si-signals").innerHTML = '<div class="empty">获取失败</div>';
    $("#si-meta").textContent = "";
    $("#si-summary").innerHTML = "";
    toast(e.message);
  }
  try {
    const info = await api("/api/strategy/info", params);
    const rows = Object.entries(info).map(([k, v]) => ({
      项: SI_INFO_LABELS[k] || k, 值: v === null || v === undefined ? "" : v,
    }));
    renderTable("#si-strategy", rows, {
      columns: ["项", "值"], noSort: new Set(["项", "值"]), noFilter: new Set(["项", "值"]),
    });
  } catch (e) {
    $("#si-strategy").innerHTML = '<div class="empty">获取失败</div>';
    toast(e.message);
  }
}
$("#si-refresh").addEventListener("click", loadStratInfo);
let siTimer = null;
$("#si-auto").addEventListener("change", (e) => {
  clearInterval(siTimer);
  if (e.target.checked) siTimer = setInterval(loadStratInfo, 15000);
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
    setSellAllEnabled(false, "该账户未配置实盘节点 API（node_api）");
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
    setSellAllEnabled(state.sellEnabled);
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
// 交易管理 → 当前目标 — the node's in-memory target book (target vs current qty).
// Reads /api/strategy/targets (live strategy memory). A row is highlighted green
// when current_qty == target_qty (已达标); frozen names are flagged separately.
// ---------------------------------------------------------------------------
const TGT_COLS = ["stock_code", "name", "target_qty", "current_qty", "diff", "frozen", "achieved"];
const TGT_HEADERS = {
  stock_code: "证券代码", name: "证券名称", target_qty: "目标数量",
  current_qty: "当前数量", diff: "差额", frozen: "冻结", achieved: "已达标",
};

async function loadTargets() {
  if (!state.account) return;
  if (!hasNodeApi()) {
    nodeApiHint("#tgt-table");
    $("#tgt-meta").textContent = "";
    return;
  }
  try {
    const t = await api("/api/strategy/targets",
      { account: state.account.account_id, trader: state.account.trader_id });
    const achieved = (t.targets || []).filter((r) => r.achieved).length;
    $("#tgt-meta").textContent =
      `版本：${t.target_version || "—"} · 目标日期：${t.target_date || "—"} · ` +
      `共 ${t.count || 0} 只，已达标 ${achieved} 只` +
      (t.achieved_version ? " · 版本已全部达标" : "");
    renderTable("#tgt-table", t.targets || [], {
      columns: TGT_COLS, headers: TGT_HEADERS,
      intCols: new Set(["target_qty", "current_qty", "diff"]),
      signCols: new Set(["diff"]),
      linkStock: true,
      rowClass: (row) => (row.achieved ? "achieved-row" : ""),
      cellFn: (c, v, row) => {
        if (c === "achieved") return `<td>${v ? "✓" : ""}</td>`;
        if (c === "frozen") return `<td class="text" title="${row.frozen_reason || ""}">${v ? "❄" : ""}</td>`;
        return undefined;
      },
    });
  } catch (e) {
    $("#tgt-table").innerHTML = '<div class="empty">获取失败</div>';
    $("#tgt-meta").textContent = "";
    toast(e.message);
  }
}
$("#tgt-refresh").addEventListener("click", loadTargets);
let tgtTimer = null;
$("#tgt-auto").addEventListener("change", (e) => {
  clearInterval(tgtTimer);
  if (e.target.checked) tgtTimer = setInterval(loadTargets, 15000);
});

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
resetMetricSelection(ASSET_METRICS);
defaultSeriesRange();
// Restore the last-visited page (from a prior refresh) before init kicks off the
// account-change flow, so data loads directly into the page the user was on.
restoreActiveNav();
initSources().catch((e) => toast("初始化失败: " + e.message));
