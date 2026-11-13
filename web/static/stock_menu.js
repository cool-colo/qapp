"use strict";

// Delegation keeps the menu working after table sorting and automatic refreshes.
(() => {
  const menu = document.createElement("div");
  menu.id = "stock-menu";
  menu.className = "stock-menu";
  menu.setAttribute("role", "menu");
  menu.hidden = true;
  document.body.appendChild(menu);
  let anchor = null, hideTimer = null;
  const observer = new MutationObserver(() => {
    if (anchor && !anchor.isConnected) hide();
  });

  function hide() {
    clearTimeout(hideTimer);
    if (anchor) anchor.setAttribute("aria-expanded", "false");
    menu.hidden = true;
    anchor = null;
    observer.disconnect();
  }

  function show(target) {
    clearTimeout(hideTimer);
    if (anchor && anchor !== target) hide();
    anchor = target;
    menu.innerHTML = `<div class="stock-menu-code"></div>
      <button type="button" role="menuitem" data-chart="kline">个股 K 线</button>
      <button type="button" role="menuitem" data-chart="minute">实时分钟线</button>`;
    menu.querySelector(".stock-menu-code").textContent = target.dataset.code;
    menu.hidden = false;
    target.setAttribute("aria-expanded", "true");
    const rect = target.getBoundingClientRect();
    menu.style.left = `${Math.max(8, Math.min(rect.left, window.innerWidth - menu.offsetWidth - 8))}px`;
    const top = rect.bottom + 6;
    menu.style.top = `${Math.max(8, top + menu.offsetHeight < window.innerHeight ? top : rect.top - menu.offsetHeight - 6)}px`;
    observer.observe(document.body, { childList: true, subtree: true });
  }

  document.addEventListener("mouseover", (event) => {
    const target = event.target.closest && event.target.closest("a.klink");
    if (!target || target.contains(event.relatedTarget)) return;
    show(target);
  });
  document.addEventListener("mouseout", (event) => {
    if (!anchor || !anchor.contains(event.target) || anchor.contains(event.relatedTarget)) return;
    if (menu.contains(event.relatedTarget)) return;
    hideTimer = setTimeout(hide, 180);
  });
  menu.addEventListener("mouseenter", () => clearTimeout(hideTimer));
  menu.addEventListener("mouseleave", () => { hideTimer = setTimeout(hide, 180); });
  document.addEventListener("click", (event) => {
    const target = event.target.closest && event.target.closest("a.klink");
    if (target) {
      event.preventDefault();
      show(target);
      menu.querySelector("button").focus();
    } else if (!menu.contains(event.target)) hide();
  });
  menu.addEventListener("click", (event) => {
    const button = event.target.closest("[data-chart]");
    if (!button || !anchor) return;
    const code = anchor.dataset.code, day = anchor.dataset.stockDate;
    hide();
    if (button.dataset.chart === "kline") openKline(code, day);
    else openRealtimeMinute(code);
  });
  document.addEventListener("keydown", (event) => {
    const target = event.target.closest && event.target.closest("a.klink");
    if (target && (event.key === "Enter" || event.key === " ")) {
      event.preventDefault(); show(target); menu.querySelector("button").focus();
    } else if (event.key === "Escape" && anchor) {
      const previous = anchor;
      hide(); previous.focus();
    } else if (!menu.hidden && menu.contains(event.target) && ["ArrowDown", "ArrowUp"].includes(event.key)) {
      event.preventDefault();
      const buttons = Array.from(menu.querySelectorAll("button"));
      const index = buttons.indexOf(document.activeElement);
      buttons[(index + (event.key === "ArrowDown" ? 1 : buttons.length - 1)) % buttons.length].focus();
    }
  });
  document.addEventListener("scroll", (event) => { if (!menu.contains(event.target)) hide(); }, true);
  window.addEventListener("resize", hide);
  window.addEventListener("blur", hide);
})();
