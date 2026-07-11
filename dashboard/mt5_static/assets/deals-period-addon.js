(() => {
  const API_BASE = "/mt5-api";
  const TOKEN_KEY = "cipherfx_mt5_token";
  const periods = [
    ["today", "Today"],
    ["week", "Week"],
    ["month", "Month"],
    ["all", "All"],
  ];
  const money = (value) => {
    const num = Number(value || 0);
    const sign = num > 0 ? "+" : num < 0 ? "-" : "";
    return `${sign}$${Math.abs(num).toFixed(2)}`;
  };
  const esc = (value) => String(value ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  function token() {
    return localStorage.getItem(TOKEN_KEY) || "";
  }

  async function fetchDeals(period) {
    const response = await fetch(`${API_BASE}/deals?period=${encodeURIComponent(period)}&limit=300`, {
      headers: { Authorization: `Bearer ${token()}` },
    });
    if (!response.ok) throw new Error(`Deals API ${response.status}`);
    return response.json();
  }

  function ensurePanel() {
    let panel = document.getElementById("cf-deals-panel");
    if (panel) return panel;
    panel = document.createElement("section");
    panel.id = "cf-deals-panel";
    panel.className = "cf-deals-panel collapsed";
    panel.innerHTML = `
      <button type="button" class="cf-deals-toggle" aria-expanded="false">
        <span>Deals P&amp;L</span><strong id="cf-deals-toggle-pnl">$0.00</strong>
      </button>
      <div class="cf-deals-body">
        <div class="cf-deals-head"><strong>MT5 Deals</strong><span id="cf-deals-range">Today</span></div>
        <div class="cf-deals-tabs" role="tablist">
          ${periods.map(([id, label]) => `<button type="button" data-period="${id}" class="${id === "today" ? "active" : ""}">${label}</button>`).join("")}
        </div>
        <div class="cf-deals-metrics">
          <div><span>P&amp;L</span><strong id="cf-deals-pnl">$0.00</strong></div>
          <div><span>Trades</span><strong id="cf-deals-count">0</strong></div>
          <div><span>Win rate</span><strong id="cf-deals-wr">0%</strong></div>
          <div><span>PF</span><strong id="cf-deals-pf">0.00</strong></div>
        </div>
        <div class="cf-deals-symbols" id="cf-deals-symbols"></div>
        <div class="cf-deals-list" id="cf-deals-list"><div class="cf-deals-empty">Loading deals...</div></div>
      </div>`;
    document.body.appendChild(panel);
    panel.querySelector(".cf-deals-toggle").addEventListener("click", () => {
      const collapsed = panel.classList.toggle("collapsed");
      panel.querySelector(".cf-deals-toggle").setAttribute("aria-expanded", String(!collapsed));
      if (!collapsed) load(panel.dataset.period || "today");
    });
    panel.querySelectorAll("[data-period]").forEach((button) => {
      button.addEventListener("click", () => load(button.dataset.period));
    });
    return panel;
  }

  function setText(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
  }

  function render(payload) {
    const panel = ensurePanel();
    const summary = payload.summary || {};
    const pnl = Number(summary.pnl || 0);
    panel.dataset.period = payload.period || "today";
    panel.querySelectorAll("[data-period]").forEach((button) => button.classList.toggle("active", button.dataset.period === payload.period));
    setText("cf-deals-toggle-pnl", money(pnl));
    setText("cf-deals-pnl", money(pnl));
    setText("cf-deals-count", String(summary.total_trades || 0));
    setText("cf-deals-wr", `${Number(summary.win_rate || 0).toFixed(1)}%`);
    setText("cf-deals-pf", Number(summary.profit_factor || 0).toFixed(2));
    setText("cf-deals-range", payload.period === "all" ? "All history" : `${payload.start_date || ""} to ${payload.end_date || ""}`);
    ["cf-deals-toggle-pnl", "cf-deals-pnl"].forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.className = pnl >= 0 ? "pos" : "neg";
    });
    const symbols = document.getElementById("cf-deals-symbols");
    if (symbols) {
      const rows = summary.by_symbol || [];
      symbols.innerHTML = rows.length ? rows.slice(0, 6).map((row) => `
        <div><span>${esc(row.symbol)}</span><strong class="${Number(row.pnl || 0) >= 0 ? "pos" : "neg"}">${money(row.pnl)}</strong><em>${row.trades} deal${row.trades === 1 ? "" : "s"}</em></div>
      `).join("") : `<div class="cf-deals-empty compact">No symbol P&amp;L for this period.</div>`;
    }
    const list = document.getElementById("cf-deals-list");
    if (list) {
      const deals = payload.deals || [];
      list.innerHTML = deals.length ? deals.map((row) => {
        const rowPnl = Number(row.pnl || row.realized || 0);
        const when = row.closed_label || row.closed_at || row.trade_date || "";
        return `<div class="cf-deal-row">
          <div><strong>${esc(row.symbol || row.sym || "MT5")}</strong><span>${esc(row.side || row.direction || row.outcome || "deal")} ${Number(row.volume || row.qty || 0).toFixed(2)}</span></div>
          <div><span>${esc(when)}</span><em>${esc(row.entry || "")} → ${esc(row.exit_px || row.exit || "")}</em></div>
          <strong class="${rowPnl >= 0 ? "pos" : "neg"}">${money(rowPnl)}</strong>
        </div>`;
      }).join("") : `<div class="cf-deals-empty">No closed deals for this selection.</div>`;
    }
  }

  async function load(period = "today") {
    const panel = ensurePanel();
    panel.dataset.period = period;
    try {
      render(await fetchDeals(period));
    } catch (error) {
      const list = document.getElementById("cf-deals-list");
      if (list) list.innerHTML = `<div class="cf-deals-empty">Sign in to load MT5 deals.</div>`;
    }
  }

  window.addEventListener("load", () => {
    ensurePanel();
    load("today");
    setInterval(() => load(document.getElementById("cf-deals-panel")?.dataset.period || "today"), 30000);
  });
})();
