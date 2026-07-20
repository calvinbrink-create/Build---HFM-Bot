import { chromium } from "playwright";
const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, ignoreHTTPSErrors: true, isMobile: true, hasTouch: true });
const page = await context.newPage();
await page.goto("https://mytradebot.co.za/cipherfx/", { waitUntil: "networkidle", timeout: 30000 });
const info = await page.evaluate(() => {
  function box(sel) {
    const el = document.querySelector(sel);
    if (!el) return null;
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    return { sel, x: r.x, y: r.y, w: r.width, h: r.height, display: cs.display, alignItems: cs.alignItems, justifyContent: cs.justifyContent, flexDirection: cs.flexDirection, width: cs.width, position: cs.position, marginLeft: cs.marginLeft, textAlign: cs.textAlign, transform: cs.transform };
  }
  return [box(".hero"), box(".hero-inner"), box(".hero-logo"), box(".shell"), box("h1"), box(".lead"), box(".hero-actions")];
});
console.log(JSON.stringify(info, null, 2));
await browser.close();
