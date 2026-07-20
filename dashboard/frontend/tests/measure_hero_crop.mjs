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
  localStorage.setItem("cipherfx_dashboard_auth_v1", JSON.stringify({ email: "mkt4@vps", expiresAt: Date.now() + 3600000, dashboards: ["mt5"] }));
}, { sessionToken: token });
const page = await context.newPage();
await page.goto(`${DASHBOARD_URL}?mkt4=${Date.now()}`, { waitUntil: "networkidle", timeout: 30000 });
await page.waitForTimeout(1500);
const box = await page.evaluate(() => {
  const els = Array.from(document.querySelectorAll("*"));
  const controlRoom = els.find(e => e.textContent.trim().startsWith("TRADING CONTROL ROOM") && e.children.length < 3);
  const target = controlRoom ? controlRoom.closest('[class*="control"], section, div') : null;
  // find the element containing "System status" heading and get its top
  const sysStatusHeading = els.find(e => e.textContent.trim() === "System status");
  const sysCard = sysStatusHeading ? sysStatusHeading.closest('div') : null;
  let sysTop = null;
  if (sysCard) {
    // walk up until we get a reasonably-sized card ancestor
    let node = sysCard;
    for (let i = 0; i < 5 && node.parentElement; i++) {
      node = node.parentElement;
    }
    sysTop = node.getBoundingClientRect().top;
  }
  return { sysTop, dpr: window.devicePixelRatio };
});
console.log(JSON.stringify(box));
await browser.close();
