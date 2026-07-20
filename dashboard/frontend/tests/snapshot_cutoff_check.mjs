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
// iPhone 15/14-class viewport (390x844) AND a taller device (iPhone SE-class 375x667, more likely to clip)
for (const [name, vw, vh] of [["iphone15", 390, 844], ["iphoneSE", 375, 667]]) {
  const context = await browser.newContext({ viewport: { width: vw, height: vh }, deviceScaleFactor: 2, ignoreHTTPSErrors: true, isMobile: true, hasTouch: true });
  await context.addInitScript(({ sessionToken }) => {
    localStorage.setItem("cipherfx_mt5_token", sessionToken);
    localStorage.setItem("cipherfx_dashboard_auth_v1", JSON.stringify({ email: "cutoff@vps", expiresAt: Date.now() + 3600000, dashboards: ["mt5"] }));
  }, { sessionToken: token });
  const page = await context.newPage();
  await page.goto(`${DASHBOARD_URL}?cutoff=${Date.now()}`, { waitUntil: "networkidle", timeout: 30000 });
  await page.waitForTimeout(2000);
  await page.screenshot({ path: `/tmp/claude-0/-root/9dca97ce-58d8-4690-a34d-0295150b0558/scratchpad/snapshot-${name}-viewport.png` });
  await page.screenshot({ path: `/tmp/claude-0/-root/9dca97ce-58d8-4690-a34d-0295150b0558/scratchpad/snapshot-${name}-full.png`, fullPage: true });
  const overflow = await page.evaluate(() => ({
    scrollHeight: document.documentElement.scrollHeight,
    clientHeight: document.documentElement.clientHeight,
    bodyOverflowY: getComputedStyle(document.body).overflowY,
    mainOverflow: document.querySelector(".dashboard-main") ? getComputedStyle(document.querySelector(".dashboard-main")).overflowY : "n/a",
  }));
  console.log(name, JSON.stringify(overflow));
  await context.close();
}
await browser.close();
