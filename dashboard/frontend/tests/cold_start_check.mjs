import { chromium } from "playwright";
const DASHBOARD_URL = "https://mytradebot.co.za/dashboard/mt5/";
const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, ignoreHTTPSErrors: true, isMobile: true,
  userAgent: "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1" });
const page = await context.newPage();
const errors = [];
const netFailures = [];
page.on("console", (msg) => { if (msg.type() === "error") errors.push(msg.text()); });
page.on("pageerror", (err) => errors.push("PAGEERROR: " + err.message + "\n" + (err.stack || "")));
page.on("requestfailed", (req) => netFailures.push(`${req.method()} ${req.url()} -> ${req.failure()?.errorText}`));
page.on("response", (res) => { if (res.status() >= 400) netFailures.push(`${res.status()} ${res.url()}`); });
// no localStorage injection - true cold start, no saved session
await page.goto(`${DASHBOARD_URL}?cold=${Date.now()}`, { waitUntil: "networkidle", timeout: 30000 });
await page.waitForTimeout(3000);
const bodyText = await page.evaluate(() => document.body.innerText.slice(0, 400));
const htmlSnippet = await page.evaluate(() => document.documentElement.outerHTML.slice(0, 800));
console.log("BODY TEXT PREVIEW:", bodyText);
console.log("ERRORS:", JSON.stringify(errors, null, 2));
console.log("NET FAILURES:", JSON.stringify(netFailures, null, 2));
await page.screenshot({ path: "/tmp/claude-0/-root/9dca97ce-58d8-4690-a34d-0295150b0558/scratchpad/cold_start.png", fullPage: true });
await browser.close();
