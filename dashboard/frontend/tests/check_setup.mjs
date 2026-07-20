import { chromium } from "playwright";
const OUT = "/tmp/claude-0/-root/9dca97ce-58d8-4690-a34d-0295150b0558/scratchpad/marketing_site";
const browser = await chromium.launch({ headless: true });

const d = await browser.newContext({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 1.5, ignoreHTTPSErrors: true });
const dp = await d.newPage();
const consoleErrors = [];
dp.on("console", (msg) => { if (msg.type() === "error") consoleErrors.push(msg.text()); });
dp.on("pageerror", (err) => consoleErrors.push("PAGEERROR: " + err.message));
await dp.goto("https://mytradebot.co.za/cipherfx/setup/", { waitUntil: "networkidle", timeout: 30000 });
await dp.waitForTimeout(1000);
await dp.screenshot({ path: `${OUT}/setup-desktop.png`, fullPage: true });
console.log("CONSOLE ERRORS:", JSON.stringify(consoleErrors));
await d.close();

const m = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, ignoreHTTPSErrors: true, isMobile: true });
const mp = await m.newPage();
await mp.goto("https://mytradebot.co.za/cipherfx/setup/", { waitUntil: "networkidle", timeout: 30000 });
await mp.waitForTimeout(1000);
await mp.screenshot({ path: `${OUT}/setup-mobile.png`, fullPage: true });
await m.close();

await browser.close();
console.log("done");
