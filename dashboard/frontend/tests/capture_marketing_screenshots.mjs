import fs from "node:fs";
import { chromium } from "playwright";

const ENV_FILE = "/etc/scalpbot/scalpbot-mt5-dashboard.env";
const DASHBOARD_URL = "https://167.233.36.200/dashboard/mt5/";
const API_URL = "http://127.0.0.1:8010/api";
const OUT_DIR = "/tmp/claude-0/-root/9dca97ce-58d8-4690-a34d-0295150b0558/scratchpad/marketing";

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
  const payload = await response.json();
  return payload.token;
}

fs.mkdirSync(OUT_DIR, { recursive: true });
const token = await dashboardToken();
const browser = await chromium.launch({ headless: true });
try {
  // 1. Desktop overview - Snapshot tab, full page for a rich "overview" shot
  {
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2, ignoreHTTPSErrors: true });
    await context.addInitScript(({ sessionToken }) => {
      localStorage.setItem("cipherfx_mt5_token", sessionToken);
      localStorage.setItem("cipherfx_dashboard_auth_v1", JSON.stringify({
        email: "marketing@vps", expiresAt: Date.now() + 3600000, dashboards: ["mt5"],
      }));
    }, { sessionToken: token });
    const page = await context.newPage();
    await page.goto(`${DASHBOARD_URL}?mkt=${Date.now()}`, { waitUntil: "networkidle", timeout: 30000 });
    await page.waitForTimeout(1500);
    await page.screenshot({ path: `${OUT_DIR}/overview-desktop.png` });
    await context.close();
  }

  // 2. Mobile - Snapshot tab
  {
    const context = await browser.newContext({ viewport: { width: 430, height: 932 }, deviceScaleFactor: 2, ignoreHTTPSErrors: true });
    await context.addInitScript(({ sessionToken }) => {
      localStorage.setItem("cipherfx_mt5_token", sessionToken);
      localStorage.setItem("cipherfx_dashboard_auth_v1", JSON.stringify({
        email: "marketing2@vps", expiresAt: Date.now() + 3600000, dashboards: ["mt5"],
      }));
    }, { sessionToken: token });
    const page = await context.newPage();
    await page.goto(`${DASHBOARD_URL}?mkt2=${Date.now()}`, { waitUntil: "networkidle", timeout: 30000 });
    await page.waitForTimeout(1500);
    await page.screenshot({ path: `${OUT_DIR}/overview-mobile.png` });
    await context.close();
  }

  // 3. Desktop - History tab (reports)
  {
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2, ignoreHTTPSErrors: true });
    await context.addInitScript(({ sessionToken }) => {
      localStorage.setItem("cipherfx_mt5_token", sessionToken);
      localStorage.setItem("cipherfx_dashboard_auth_v1", JSON.stringify({
        email: "marketing3@vps", expiresAt: Date.now() + 3600000, dashboards: ["mt5"],
      }));
    }, { sessionToken: token });
    const page = await context.newPage();
    await page.goto(`${DASHBOARD_URL}?mkt3=${Date.now()}`, { waitUntil: "networkidle", timeout: 30000 });
    await page.waitForTimeout(1200);
    const btn = page.locator(".bottom-nav button").filter({ has: page.locator(`.desktop-label:text-is("History")`) });
    await btn.first().click();
    await page.waitForTimeout(1000);
    await page.screenshot({ path: `${OUT_DIR}/reports-desktop.png` });
    await context.close();
  }

  console.log("DONE");
} finally {
  await browser.close();
}
