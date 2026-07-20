// Smoke test for the LIVE rebuilt MT5 dashboard (bottom-nav / sidebar UI).
// Replaces the legacy test that targeted the retired side-tab Scanner UI.
// Must pass on weekdays and weekends: no assumptions about live ticks.
import fs from "node:fs";
import path from "node:path";
import { chromium } from "playwright";

const ENV_FILE = "/etc/scalpbot/scalpbot-mt5-dashboard.env";
const DASHBOARD_URL = "https://167.233.36.200/dashboard/mt5/";
const API_URL = "http://127.0.0.1:8010/api";
const ARTIFACTS = "/opt/cipherfx_mt5/dashboard/frontend/tests/artifacts";

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
    body: JSON.stringify({ email: env.MT5_LOGIN, password: env.MT5_PASSWORD }),
  });
  if (!response.ok) throw new Error(`login failed: ${response.status}`);
  const payload = await response.json();
  if (!payload.token) throw new Error("login returned no token");
  return payload.token;
}

async function verifyViewport(browser, token, name, viewport, mobile) {
  const context = await browser.newContext({
    viewport,
    deviceScaleFactor: 2,
    ignoreHTTPSErrors: true,
    ...(mobile ? { isMobile: true, hasTouch: true } : {}),
  });
  await context.addInitScript(({ sessionToken }) => {
    localStorage.setItem("cipherfx_mt5_token", sessionToken);
    localStorage.setItem("cipherfx_dashboard_auth_v1", JSON.stringify({
      email: "smoke@vps",
      expiresAt: Date.now() + 3600000,
      dashboards: ["mt5"],
    }));
  }, { sessionToken: token });

  const page = await context.newPage();
  const consoleErrors = [];
  page.on("console", (msg) => { if (msg.type() === "error") consoleErrors.push(msg.text()); });
  page.on("pageerror", (err) => consoleErrors.push("PAGEERROR: " + err.message));

  const statusResponse = page.waitForResponse(
    (response) => response.url().includes("/mt5-api/status") && response.status() === 200,
    { timeout: 30000 },
  );
  await page.goto(`${DASHBOARD_URL}?live-smoke=${Date.now()}`, {
    waitUntil: "domcontentloaded",
    timeout: 30000,
  });
  await statusResponse;
  await page.waitForTimeout(1500);

  // Snapshot tab is the landing view.
  const snapshotText = await page.evaluate(() => document.body.innerText);
  for (const expected of ["System status", "Market scanner", "TRADING CONTROL ROOM"]) {
    if (!snapshotText.includes(expected)) throw new Error(`${name}: missing Snapshot text: ${expected}`);
  }

  // The bottom-nav must expose all eight tabs exactly once each.
  const tabNames = ["Snapshot", "Market", "Chart", "Replay", "Scan", "Intel", "Trade", "History"];
  for (const tab of tabNames) {
    const count = await page.locator(".bottom-nav button").filter({
      has: page.locator(`.desktop-label:text-is("${tab}")`),
    }).count();
    if (count !== 1) throw new Error(`${name}: tab "${tab}" not unique (found ${count})`);
  }

  // Walk the core tabs and confirm each renders its workspace.
  const tabChecks = [
    { tab: "Market", expect: "Market watch" },
    { tab: "Chart", expect: "live chart" },
    { tab: "History", expect: "MT5 trade history" },
  ];
  for (const { tab, expect } of tabChecks) {
    const button = page.locator(".bottom-nav button").filter({
      has: page.locator(`.desktop-label:text-is("${tab}")`),
    });
    await button.first().click();
    await page.waitForTimeout(1800);
    const text = await page.evaluate(() => document.body.innerText);
    if (!text.toLowerCase().includes(expect.toLowerCase())) {
      throw new Error(`${name}: ${tab} tab missing text: ${expect}`);
    }
  }

  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
  );
  if (mobile && overflow) throw new Error(`${name}: mobile page has horizontal overflow`);

  const fatalErrors = consoleErrors.filter((line) => !/favicon|manifest/i.test(line));
  if (fatalErrors.length) throw new Error(`${name}: console errors: ${fatalErrors.slice(0, 3).join(" | ")}`);

  const screenshot = path.join(ARTIFACTS, `dashboard-${name}.png`);
  await page.screenshot({ path: screenshot, fullPage: true });
  await context.close();
  return { viewport: name, console_errors: 0, horizontal_overflow: overflow, screenshot };
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
