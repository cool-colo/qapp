"use strict";

// Delegated handlers also cover rows replaced by sorting, filtering or refreshes.
(() => {
  const HOVER_DELAY_MS = 300;
  const panel = document.createElement("div");
  panel.id = "stock-hover-panel";
  panel.className = "stock-hover-panel";
  panel.setAttribute("role", "tooltip");
  panel.setAttribute("aria-live", "polite");
  panel.hidden = true;
  document.body.appendChild(panel);

  let anchor = null, showTimer = null, hideTimer = null, request = null;
  const observer = new MutationObserver(() => {
    if (anchor && !anchor.isConnected) dismiss();
  });

  const escape = (value) => String(value ?? "").replace(/[&<>"']/g,
    (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  const number = (value, digits = 2) => typeof value === "number" && Number.isFinite(value)
    ? value.toLocaleString("zh-CN", { minimumFractionDigits: digits, maximumFractionDigits: digits }) : "—";
  const rate = (value) => typeof value === "number" && Number.isFinite(value) ? `${number(value * 100)}%` : "—";
  const money = (value) => {
    if (typeof value !== "number" || !Number.isFinite(value)) return "—";
    if (Math.abs(value) >= 1e8) return `${number(value / 1e8)} 亿`;
    if (Math.abs(value) >= 1e4) return `${number(value / 1e4)} 万`;
    return `${number(value)} 元`;
  };

  function dismiss() {
    clearTimeout(showTimer);
    clearTimeout(hideTimer);
    if (request) request.abort();
    request = null;
    if (anchor) anchor.removeAttribute("aria-describedby");
    anchor = null;
    panel.hidden = true;
    observer.disconnect();
  }

  function position() {
    if (!anchor || panel.hidden) return;
    const rect = anchor.getBoundingClientRect();
    const gap = 8, margin = 12, horizontalOffset = 16;
    const width = panel.offsetWidth, height = panel.offsetHeight;
    const belowTop = rect.bottom + gap;
    const aboveTop = rect.top - height - gap;
    const rightLeft = rect.right + gap;
    const leftLeft = rect.left - width - gap;

    if (belowTop + height <= window.innerHeight - margin) {
      panel.style.left = `${Math.max(margin, Math.min(rect.left + horizontalOffset, window.innerWidth - width - margin))}px`;
      panel.style.top = `${belowTop}px`;
    } else if (aboveTop >= margin) {
      panel.style.left = `${Math.max(margin, Math.min(rect.left + horizontalOffset, window.innerWidth - width - margin))}px`;
      panel.style.top = `${aboveTop}px`;
    } else if (rightLeft + width <= window.innerWidth - margin) {
      panel.style.left = `${rightLeft}px`;
      panel.style.top = `${Math.max(margin, Math.min(rect.top, window.innerHeight - height - margin))}px`;
    } else {
      panel.style.left = `${Math.max(margin, leftLeft)}px`;
      panel.style.top = `${Math.max(margin, Math.min(rect.top, window.innerHeight - height - margin))}px`;
    }
  }

  function heading(name, code) {
    return `<div class="stock-hover-heading"><strong>${escape(name)}</strong><span>${escape(code)}</span></div>`;
  }

  function render(info, name) {
    const fields = [
      ["总市值", money(info.total_market_cap)],
      ["流通市值", money(info.circulating_market_cap)],
      ["换手率", rate(info.turnover_rate)],
      ["自由流通换手率", rate(info.free_float_turnover_rate)],
      ["市盈率 TTM", number(info.pe_ttm)],
      ["市净率", number(info.pb)],
      ["量比", number(info.volume_ratio)],
      ["成交额", money(info.amount)],
      ["成交量（股）", number(info.volume, 0)],
      ["昨收", number(info.previous_close)],
      ["开盘", number(info.open)],
      ["最高 / 最低", `${number(info.high)} / ${number(info.low)}`],
    ];
    const change = info.daily_change;
    const sign = typeof change === "number" ? (change > 0 ? "pos" : change < 0 ? "neg" : "zero") : "";
    const details = [info.industry, info.area, info.market].filter(Boolean).map(escape).join(" · ");
    panel.innerHTML = heading(info.stock_name || name, info.stock_code)
      + `<div class="stock-hover-meta">${details || "—"}</div>`
      + (info.trade_date
        ? `<div class="stock-hover-quote"><strong class="${sign}">${number(info.close)}</strong><span class="${sign}">${rate(change)}</span><span>收盘 / 涨跌幅</span></div>`
          + `<dl class="stock-hover-metrics">${fields.map(([label, value]) => `<div><dt>${label}</dt><dd>${value}</dd></div>`).join("")}</dl>`
        : `<div class="stock-hover-message">暂无截至 ${escape(info.as_of)} 的日线数据</div>`)
      + `<div class="stock-hover-footer">上市日期：${escape(info.listed_date ? String(info.listed_date).slice(0, 10) : "—")}<br>`
      + `日线数据：${escape(info.trade_date ? String(info.trade_date).slice(0, 10) : "—")}</div>`;
    position();
  }

  async function show(target) {
    if (anchor !== target || !target.isConnected) return;
    const code = target.dataset.code, name = target.textContent;
    panel.innerHTML = heading(name, code) + '<div class="stock-hover-message">加载中…</div>';
    panel.hidden = false;
    target.setAttribute("aria-describedby", panel.id);
    position();
    const controller = new AbortController();
    request = controller;
    try {
      const info = await api("/api/stock_info", { stock_code: code, date: target.dataset.stockDate }, { signal: controller.signal });
      if (anchor === target && request === controller && target.isConnected) render(info, name);
    } catch (error) {
      if (anchor !== target || request !== controller || error.name === "AbortError") return;
      panel.innerHTML = heading(name, code) + '<div class="stock-hover-message">暂时无法获取股票信息，请稍后重试</div>';
      position();
    }
  }

  function scheduleHide() {
    clearTimeout(showTimer);
    if (panel.hidden) dismiss();
    else hideTimer = setTimeout(dismiss, 150); // Allow crossing the gap into the panel.
  }

  document.addEventListener("mouseover", (event) => {
    if (!(event.target instanceof Element)) return;
    const target = event.target.closest(".stock-hover-trigger");
    if (!target || target.contains(event.relatedTarget)) return;
    clearTimeout(hideTimer);
    if (anchor === target) return;
    dismiss();
    anchor = target;
    observer.observe(document.body, { childList: true, subtree: true });
    showTimer = setTimeout(() => show(target), HOVER_DELAY_MS);
  });
  document.addEventListener("mouseout", (event) => {
    if (!anchor || !(event.target instanceof Element)) return;
    if (event.target.closest(".stock-hover-trigger") !== anchor) return;
    if (anchor.contains(event.relatedTarget)) return;
    if (event.relatedTarget && panel.contains(event.relatedTarget)) clearTimeout(hideTimer);
    else scheduleHide();
  });
  panel.addEventListener("mouseenter", () => clearTimeout(hideTimer));
  panel.addEventListener("mouseleave", scheduleHide);
  document.addEventListener("click", dismiss, true);
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") dismiss(); });
  document.addEventListener("scroll", (event) => { if (!panel.contains(event.target)) dismiss(); }, true);
  window.addEventListener("resize", dismiss);
  window.addEventListener("blur", dismiss);
})();
