import { chromium } from "playwright";
const OUT = "/tmp/claude-0/-root/9dca97ce-58d8-4690-a34d-0295150b0558/scratchpad/marketing_site";
const browser = await chromium.launch({ headless: true });

const d = await browser.newContext({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 1.5, ignoreHTTPSErrors: true });
const dp = await d.newPage();
await dp.goto("https://mytradebot.co.za/cipherfx/#dashboard-preview", { waitUntil: "networkidle", timeout: 30000 });
await dp.locator("#dashboard-preview").scrollIntoViewIfNeeded();
await dp.waitForTimeout(800);
await dp.screenshot({ path: `${OUT}/preview-desktop.png` });
await d.close();

const m = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, ignoreHTTPSErrors: true, isMobile: true });
const mp = await m.newPage();
await mp.goto("https://mytradebot.co.za/cipherfx/#dashboard-preview", { waitUntil: "networkidle", timeout: 30000 });
await mp.locator("#dashboard-preview").scrollIntoViewIfNeeded();
await mp.waitForTimeout(800);
await mp.screenshot({ path: `${OUT}/preview-mobile.png` });
await m.close();

await browser.close();
console.log("done");
