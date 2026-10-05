// Preview the hero (fog + clickable demo) from disk: desktop, the quiz page with an answer revealed, and phone.
import { chromium } from "playwright-core";
const OUT = process.env.OUT, SRC = "file://" + new URL("../static/index.html", import.meta.url).pathname;
const b = await chromium.launch({ channel: "chrome" });
const errs = [];
const d = await b.newPage({ viewport: { width: 1440, height: 900 } });
d.on("pageerror", e => errs.push(String(e)));
await d.goto(SRC); await d.waitForTimeout(3500);
await d.screenshot({ path: `${OUT}/demo-desktop.png` });
await d.click('.d-tabs button[data-p="3"]'); await d.waitForTimeout(700);
await d.click(".pd-quiz li:first-child button"); await d.waitForTimeout(500);
await d.screenshot({ path: `${OUT}/demo-quiz.png`, clip: { x: 720, y: 60, width: 720, height: 780 } });
const m = await b.newPage({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true });
m.on("pageerror", e => errs.push("phone " + e));
await m.goto(SRC); await m.waitForTimeout(3500);
await m.screenshot({ path: `${OUT}/demo-phone.png`, fullPage: true });
console.log("phone overflow px:", await m.evaluate(() => document.documentElement.scrollWidth - innerWidth), "| js errors:", errs.length ? errs : "none");
await b.close();
