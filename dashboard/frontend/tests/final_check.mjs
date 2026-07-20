import { chromium } from "playwright";
const OUT = "/tmp/claude-0/-root/9dca97ce-58d8-4690-a34d-0295150b0558/scratchpad/marketing_site";
const browser = await chromium.launch({ headless: true });
const pages = [
  { url: "https://mytradebot.co.za/cipherfx/", name: "home-nav" },
  { url: "https://mytradebot.co.za/cipherfx/pricing/", name: "pricing" },
  { url: "https://mytradebot.co.za/cipherfx/login/", name: "login" },
];
for (const p of pages) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 700 }, deviceScaleFactor: 1.5, ignoreHTTPSErrors: true });
  const page = await ctx.newPage();
  await page.goto(p.url, { waitUntil: "networkidle", timeout: 30000 });
  await page.waitForTimeout(500);
  await page.screenshot({ path: `${OUT}/${p.name}.png` });
  await ctx.close();
}
await browser.close();
console.log("done");
