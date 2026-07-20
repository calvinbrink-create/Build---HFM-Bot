import fs from "node:fs";
import { chromium } from "playwright";
const ENV_FILE = "/etc/scalpbot/scalpbot-mt5-dashboard.env";
const DASHBOARD_URL = "https://167.233.36.200/dashboard/mt5/";
const API_URL = "http://127.0.0.1:8010/api";
const OUT = "/tmp/claude-0/-root/9dca97ce-58d8-4690-a34d-0295150b0558/scratchpad/marketing";
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
  const response = await fetch(`${API_URL}/login`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email: env.MT5_LOGIN, password: env.MT5_PASSWORD }) });
  return (await response.json()).token;
}
const token = await dashboardToken();
const browser = await chromium.launch({ headless: true });
const errors = [];

async function openTab(context, tabName, fname) {
  const page = await context.newPage();
  page.on("console", (msg) => { if (msg.type() === "error") errors.push(`[${tabName}/${fname}] ` + msg.text()); });
  page.on("pageerror", (err) => errors.push(`[${tabName}/${fname}] PAGEERROR: ` + err.message));
  await page.goto(`${DASHBOARD_URL}?vf=${Date.now()}${Math.random()}`, { waitUntil: "networkidle", timeout: 30000 });
  await page.waitForTimeout(1000);
  const btn = page.locator(".bottom-nav button").filter({ has: page.locator(`.desktop-label:text-is("${tabName}")`) });
  await btn.first().click();
  await page.waitForTimeout(2200);
  await page.screenshot({ path: `${OUT}/${fname}.png`, fullPage: fname.includes("mobile") });
  await page.close();
}

// Desktop
const d = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1.5, ignoreHTTPSErrors: true });
await d.addInitScript(({ sessionToken }) => {
  localStorage.setItem("cipherfx_mt5_token", sessionToken);
  localStorage.setItem("cipherfx_dashboard_auth_v1", JSON.stringify({ email: "verify1@vps", expiresAt: Date.now() + 3600000, dashboards: ["mt5"] }));
}, { sessionToken: token });
await openTab(d, "Replay", "replay-desktop-fixed");
await openTab(d, "Market", "market-desktop-fixed");
await d.close();

// Mobile
const m = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, ignoreHTTPSErrors: true, isMobile: true });
await m.addInitScript(({ sessionToken }) => {
  localStorage.setItem("cipherfx_mt5_token", sessionToken);
  localStorage.setItem("cipherfx_dashboard_auth_v1", JSON.stringify({ email: "verify2@vps", expiresAt: Date.now() + 3600000, dashboards: ["mt5"] }));
}, { sessionToken: token });
await openTab(m, "Replay", "replay-mobile-fixed");
await openTab(m, "Chart", "chart-mobile-fixed");
await m.close();

console.log("CONSOLE ERRORS:", JSON.stringify(errors));
await browser.close();
console.log("done");
