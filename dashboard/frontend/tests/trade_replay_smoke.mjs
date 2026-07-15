import fs from "node:fs";
import { chromium } from "playwright";

const dashboardEnv = "/etc/scalpbot/scalpbot-mt5-dashboard.env";
const dashboardUrl = "https://167.233.36.200/dashboard/mt5/";
const apiUrl = "http://127.0.0.1:8010/api";

function loadEnv(path) {
  return Object.fromEntries(fs.readFileSync(path, "utf8").split(/\r?\n/).filter((line) => line && !line.startsWith("#") && line.includes("=")).map((line) => {
    const index = line.indexOf("=");
    return [line.slice(0, index), line.slice(index + 1).replace(/^['"]|['"]$/g, "")];
  }));
}

const env = loadEnv(dashboardEnv);
const login = await fetch(apiUrl + "/login", {
  method: "POST",
  headers: {"content-type": "application/json"},
  body: JSON.stringify({email: env.MT5_LOGIN, password: env.MT5_PASSWORD}),
});
if (!login.ok) throw new Error("dashboard login failed");
const {token} = await login.json();
const browser = await chromium.launch({headless: true});

for (const [name, viewport, mobile] of [
  ["desktop", {width: 1440, height: 900}, false],
  ["mobile", {width: 390, height: 844}, true],
]) {
  const context = await browser.newContext({ignoreHTTPSErrors: true, viewport});
  await context.addInitScript(({sessionToken}) => {
    localStorage.setItem("cipherfx_mt5_token", sessionToken);
    localStorage.setItem("cipherfx_dashboard_auth_v1", JSON.stringify({
      expiresAt: Date.now() + 3600000,
      dashboards: ["mt5"],
    }));
  }, {sessionToken: token});
  const page = await context.newPage();
  const failures = [];
  const errors = [];
  page.on("response", (response) => {
    if (response.url().includes("/mt5-api/") && response.status() >= 400) failures.push(response.status());
  });
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(dashboardUrl + "?trade-replay-smoke=" + Date.now(), {waitUntil: "networkidle", timeout: 30000});
  const navigation = mobile
    ? page.getByRole("button", {name: /Replay/i}).first()
    : page.locator(".side-tab").filter({hasText: "Replay"});
  if (await navigation.count() !== 1) throw new Error(name + ": Replay navigation missing");
  await navigation.click();
  await page.locator(".replay-workspace").waitFor({state: "visible", timeout: 15000});
  await page.locator(".trade-replay-chart").waitFor({state: "visible", timeout: 15000});
  const range = page.locator('input[aria-label="Replay position"]');
  const before = await range.inputValue();
  await page.getByRole("button", {name: "Step one candle"}).click();
  const after = await range.inputValue();
  const view = await page.evaluate(() => ({
    canvas: Boolean(document.querySelector(".trade-replay-canvas")),
    safety: document.querySelector(".replay-safety-label")?.textContent,
    overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
  }));
  if (failures.length) throw new Error(name + ": API failures " + failures.join(","));
  if (errors.length) throw new Error(name + ": browser errors " + errors.join(" | "));
  if (view.safety !== "REPLAY ONLY") throw new Error(name + ": safety label missing");
  if (before === after && Number(after) < 1) throw new Error(name + ": step did not advance");
  if (mobile && view.overflow) throw new Error(name + ": horizontal overflow");
  console.log(JSON.stringify({name, before, after, ...view}));
  await context.close();
}
await browser.close();
console.log("PASS trade replay smoke");
