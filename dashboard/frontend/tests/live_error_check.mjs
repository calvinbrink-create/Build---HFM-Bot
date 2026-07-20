import fs from "node:fs";
import { chromium } from "playwright";
const ENV_FILE = "/etc/scalpbot/scalpbot-mt5-dashboard.env";
const DASHBOARD_URL = "https://mytradebot.co.za/dashboard/mt5/";
const API_URL = "http://127.0.0.1:8010/api";
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
const context = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, ignoreHTTPSErrors: true, isMobile: true });
await context.addInitScript(({ sessionToken }) => {
  localStorage.setItem("cipherfx_mt5_token", sessionToken);
  localStorage.setItem("cipherfx_dashboard_auth_v1", JSON.stringify({ email: "err@vps", expiresAt: Date.now() + 3600000, dashboards: ["mt5"] }));
}, { sessionToken: token });
const page = await context.newPage();
const errors = [];
const netFailures = [];
page.on("console", (msg) => { if (msg.type() === "error") errors.push(msg.text()); });
page.on("pageerror", (err) => errors.push("PAGEERROR: " + err.message + "\n" + (err.stack || "")));
page.on("requestfailed", (req) => netFailures.push(`${req.method()} ${req.url()} -> ${req.failure()?.errorText}`));
page.on("response", (res) => { if (res.status() >= 400) netFailures.push(`${res.status()} ${res.url()}`); });
await page.goto(`${DASHBOARD_URL}?errcheck=${Date.now()}`, { waitUntil: "networkidle", timeout: 30000 });
await page.waitForTimeout(3000);
// click through all 8 tabs
const tabs = ["Snapshot","Market","Chart","Replay","Scan","Intel","Trade","History"];
for (const tab of tabs) {
  try {
    const btn = page.locator(".bottom-nav button").filter({ has: page.locator(`.desktop-label:text-is("${tab}")`) });
    await btn.first().click({ timeout: 5000 });
    await page.waitForTimeout(1500);
  } catch (e) {
    errors.push(`TAB_CLICK_FAILED[${tab}]: ${e.message}`);
  }
}
console.log("CONSOLE/PAGE ERRORS:", JSON.stringify(errors, null, 2));
console.log("NETWORK FAILURES:", JSON.stringify(netFailures, null, 2));
await browser.close();
