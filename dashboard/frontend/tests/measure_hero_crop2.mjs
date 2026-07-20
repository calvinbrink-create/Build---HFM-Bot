import fs from "node:fs";
import { chromium } from "playwright";
const ENV_FILE = "/etc/scalpbot/scalpbot-mt5-dashboard.env";
const DASHBOARD_URL = "https://167.233.36.200/dashboard/mt5/";
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
const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2, ignoreHTTPSErrors: true });
await context.addInitScript(({ sessionToken }) => {
  localStorage.setItem("cipherfx_mt5_token", sessionToken);
  localStorage.setItem("cipherfx_dashboard_auth_v1", JSON.stringify({ email: "mkt5@vps", expiresAt: Date.now() + 3600000, dashboards: ["mt5"] }));
}, { sessionToken: token });
const page = await context.newPage();
await page.goto(`${DASHBOARD_URL}?mkt5=${Date.now()}`, { waitUntil: "networkidle", timeout: 30000 });
await page.waitForTimeout(1500);
const info = await page.evaluate(() => {
  function findByText(txt) {
    return Array.from(document.querySelectorAll("*")).find(e => e.children.length === 0 && e.textContent.trim() === txt);
  }
  const sys = findByText("System status");
  const r = sys ? sys.getBoundingClientRect() : null;
  return r ? { top: r.top, left: r.left } : null;
});
console.log(JSON.stringify(info));
await page.screenshot({ path: "/tmp/claude-0/-root/9dca97ce-58d8-4690-a34d-0295150b0558/scratchpad/marketing/overview-desktop-fresh.png" });
await browser.close();
