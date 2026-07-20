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
const context = await browser.newContext({ viewport: { width: 375, height: 667 }, deviceScaleFactor: 2, ignoreHTTPSErrors: true, isMobile: true, hasTouch: true });
await context.addInitScript(({ sessionToken }) => {
  localStorage.setItem("cipherfx_mt5_token", sessionToken);
  localStorage.setItem("cipherfx_dashboard_auth_v1", JSON.stringify({ email: "scroll@vps", expiresAt: Date.now() + 3600000, dashboards: ["mt5"] }));
}, { sessionToken: token });
const page = await context.newPage();
await page.goto(`${DASHBOARD_URL}?scr=${Date.now()}`, { waitUntil: "networkidle", timeout: 30000 });
await page.waitForTimeout(2000);
const info = await page.evaluate(() => {
  const main = document.querySelector(".dashboard-main");
  if (!main) return { found: false };
  const cs = getComputedStyle(main);
  return {
    found: true,
    scrollHeight: main.scrollHeight,
    clientHeight: main.clientHeight,
    canScroll: main.scrollHeight > main.clientHeight,
    overflowY: cs.overflowY,
    display: cs.display,
    flex: cs.flex,
    minHeight: cs.minHeight,
    height: cs.height,
    childCount: main.children.length,
  };
});
console.log(JSON.stringify(info, null, 2));
// try actually scrolling it and see if content moves
await page.evaluate(() => document.querySelector(".dashboard-main")?.scrollTo(0, 999));
await page.waitForTimeout(300);
const afterScroll = await page.evaluate(() => document.querySelector(".dashboard-main")?.scrollTop);
console.log("scrollTop after scrollTo(0,999):", afterScroll);
await page.screenshot({ path: "/tmp/claude-0/-root/9dca97ce-58d8-4690-a34d-0295150b0558/scratchpad/after-scroll-attempt.png" });
await browser.close();
