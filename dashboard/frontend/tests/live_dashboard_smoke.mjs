import fs from "node:fs";
import path from "node:path";
import { chromium } from "playwright";

const ROOT = "/opt/cipherfx_mt5";
const ENV_FILE = "/etc/scalpbot/scalpbot-mt5-dashboard.env";
const DASHBOARD_URL = "https://167.233.36.200/dashboard/mt5/";
const API_URL = "http://127.0.0.1:8010/api";
const ARTIFACTS = path.join(ROOT, "audit_artifacts");

function loadEnv(file) {
  const values = {};
  for (const rawLine of fs.readFileSync(file, "utf8").split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#") || !line.includes("=")) continue;
    const splitAt = line.indexOf("=");
    values[line.slice(0, splitAt)] = line.slice(splitAt + 1).replace(/^['"]|['"]$/g, "");
  }
  return values;
}

async function dashboardToken() {
  const env = loadEnv(ENV_FILE);
  const response = await fetch(`${API_URL}/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      email: env.MT5_LOGIN,
      password: env.MT5_PASSWORD,
    }),
  });
  if (!response.ok) throw new Error(`Dashboard login failed: HTTP ${response.status}`);
  const payload = await response.json();
  if (!payload.token) throw new Error("Dashboard login returned no token");
  return payload.token;
}

async function verifyViewport(browser, token, name, viewport, mobile) {
  const context = await browser.newContext({ ignoreHTTPSErrors: true, viewport });
  await context.addInitScript(({ sessionToken }) => {
    localStorage.setItem("cipherfx_mt5_token", sessionToken);
    localStorage.setItem("cipherfx_dashboard_auth_v1", JSON.stringify({
      email: "live-smoke@vps",
      expiresAt: Date.now() + 60 * 60 * 1000,
      dashboards: ["mt5"],
    }));
  }, { sessionToken: token });
  const page = await context.newPage();
  const apiFailures = [];
  page.on("response", (response) => {
    if (response.url().includes("/mt5-api/") && response.status() >= 400) {
      apiFailures.push(`${response.status()} ${new URL(response.url()).pathname}`);
    }
  });
  const statusResponse = page.waitForResponse(
    (response) => response.url().includes("/mt5-api/status") && response.status() === 200,
    { timeout: 30000 },
  );
  const scannerResponse = page.waitForResponse(
    (response) => response.url().includes("/mt5-api/scanner") && response.status() === 200,
    { timeout: 30000 },
  );
  const auditResponse = page.waitForResponse(
    (response) => response.url().includes("/mt5-api/audit/summary") && response.status() === 200,
    { timeout: 30000 },
  );

  await page.goto(`${DASHBOARD_URL}?live-smoke=${Date.now()}`, {
    waitUntil: "domcontentloaded",
    timeout: 30000,
  });
  const [, scannerHttp, auditHttp] = await Promise.all([statusResponse, scannerResponse, auditResponse]);
  const scannerPayload = await scannerHttp.json();
  const auditPayload = await auditHttp.json();

  const scannerButton = mobile
    ? page.locator('button[aria-label="Open Scan"]')
    : page.locator(".side-tab").filter({ hasText: "Scanner" });
  if (await scannerButton.count() !== 1) throw new Error(`${name}: scanner navigation is not unique`);
  await scannerButton.click();
  await page.locator(".scanner-workspace").waitFor({ state: "visible", timeout: 15000 });
  await page.locator(".scanner-row").first().waitFor({ state: "visible", timeout: 15000 });

  const rendered = await page.evaluate(() => ({
    scoreRows: document.querySelectorAll(".scanner-row").length,
    blockRows: document.querySelectorAll(".reason-summary-row, .scanner-event-row").length,
    metricCards: document.querySelectorAll(".scanner-card").length,
    marketSessions: document.querySelectorAll(".scanner-session").length,
    openSessions: document.querySelectorAll(".scanner-session.open").length,
    closedSessions: document.querySelectorAll(".scanner-session.closed").length,
    text: document.querySelector(".scanner-workspace")?.innerText || "",
    marketBadges: document.querySelectorAll(".scanner-state").length,
    duplicateClosureBanners: document.querySelectorAll(".market-closed-inline").length,
    horizontalOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
  }));
  const currentRows = scannerPayload.current_signals?.length || scannerPayload.recent_events?.length || 0;
  const historicalRows = auditPayload.blocks?.length || 0;

  if (currentRows < 1) throw new Error(`${name}: scanner API returned no current rows`);
  if (historicalRows < 1) throw new Error(`${name}: audit API returned no historical block rows`);
  if (rendered.scoreRows < 1) throw new Error(`${name}: no score rows rendered`);
  if (rendered.blockRows < 1) throw new Error(`${name}: no historical rows rendered`);
  if (rendered.metricCards < 6) throw new Error(`${name}: scanner metrics did not render`);
  if (rendered.marketSessions !== 4) throw new Error(`${name}: expected four market sessions`);
  if (rendered.openSessions + rendered.closedSessions !== 4) throw new Error(`${name}: market session status is incomplete`);
  if (rendered.marketBadges !== 1) throw new Error(`${name}: expected one market state badge`);
  if (rendered.duplicateClosureBanners !== 0) throw new Error(`${name}: duplicate market closure banner rendered`);
  const expectedLabels = ["Cipher FX Scoring Metric V1", "Current Scores", "Latest No-Trade Detail", "US / New York", "UK / London", "Sydney", "Asia / Tokyo"];
  if (auditPayload.market?.weekend_closed) expectedLabels.push("MARKETS CLOSED");
  for (const expected of expectedLabels) {
    if (!rendered.text.includes(expected)) throw new Error(`${name}: missing visible text: ${expected}`);
  }
  if (apiFailures.length) throw new Error(`${name}: API failures: ${apiFailures.join(", ")}`);
  if (mobile && rendered.horizontalOverflow) throw new Error(`${name}: mobile page has horizontal overflow`);

  const screenshot = path.join(ARTIFACTS, `dashboard-${name}.png`);
  await page.screenshot({ path: screenshot, fullPage: true });
  let marketHoursScreenshot = "";
  if (!mobile) {
    const marketHoursButton = page.locator(".side-tab").filter({ hasText: "Market Hours" });
    if (await marketHoursButton.count() !== 1) throw new Error(`${name}: market hours navigation is not unique`);
    await marketHoursButton.click();
    await page.locator(".market-hours-workspace").waitFor({ state: "visible", timeout: 15000 });
    const marketHoursView = await page.evaluate(() => ({
      badges: document.querySelectorAll(".market-hours-state").length,
      banners: document.querySelectorAll(".market-closed-banner").length,
      sessions: document.querySelectorAll(".market-session-row").length,
    }));
    if (marketHoursView.badges !== 1) throw new Error(`${name}: market hours must have one state badge`);
    if (marketHoursView.banners !== 0) throw new Error(`${name}: market hours contains a duplicate closure banner`);
    if (marketHoursView.sessions !== 4) throw new Error(`${name}: market hours must list four sessions`);
    marketHoursScreenshot = path.join(ARTIFACTS, "dashboard-market-hours-desktop.png");
    await page.screenshot({ path: marketHoursScreenshot, fullPage: true });
  }
  await context.close();
  return {
    viewport: name,
    current_api_rows: currentRows,
    historical_api_rows: historicalRows,
    rendered_score_rows: rendered.scoreRows,
    rendered_history_rows: rendered.blockRows,
    metric_cards: rendered.metricCards,
    horizontal_overflow: rendered.horizontalOverflow,
    screenshot,
    market_hours_screenshot: marketHoursScreenshot,
  };
}
fs.mkdirSync(ARTIFACTS, { recursive: true });
const token = await dashboardToken();
const browser = await chromium.launch({ headless: true });
try {
  const desktop = await verifyViewport(browser, token, "desktop-1440x900", { width: 1440, height: 900 }, false);
  const mobile = await verifyViewport(browser, token, "mobile-390x844", { width: 390, height: 844 }, true);
  console.log(JSON.stringify({ pass: true, desktop, mobile }, null, 2));
} finally {
  await browser.close();
}
