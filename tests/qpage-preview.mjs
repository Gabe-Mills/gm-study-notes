// Fill a LOCAL queue with a few people, then screenshot the queue page on desktop and phone.
// Usage: OUT=dir BASE=http://127.0.0.1:5211 node qpage-preview.mjs
import { chromium } from "playwright-core";
const OUT = process.env.OUT, BASE = process.env.BASE || "http://127.0.0.1:5211";

for (let i = 0; i < 30; i++) { try { await fetch(`${BASE}/api/queue`); break; } catch { await new Promise(r => setTimeout(r, 1000)); } }
const join = async (kind, ip) => (await (await fetch(`${BASE}/api/queue`, {
  method: "POST", headers: { "Content-Type": "application/json", "cf-connecting-ip": ip },
  body: JSON.stringify({ url: "https://youtu.be/jNQXAC9IVRw", kind }) })).json()).id;
const ids = [];
for (const [i, kind] of ["notes", "transcript", "notes", "transcript", "notes", "transcript"].entries()) ids.push(await join(kind, `10.0.0.${i + 1}`));
// Keep everyone "present" so nobody is dropped from the line during the preview.
const keep = setInterval(() => ids.forEach(id => fetch(`${BASE}/api/ticket/${id}`).catch(() => {})), 5000);

const b = await chromium.launch({ channel: "chrome" });
const errs = [];
const d = await b.newPage({ viewport: { width: 1440, height: 900 } });
d.on("pageerror", e => errs.push(String(e)));
await d.goto(`${BASE}/q/${ids[3]}`); await d.waitForTimeout(5000);
await d.screenshot({ path: `${OUT}/qpage-desktop.png` });
const m = await b.newPage({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true });
m.on("pageerror", e => errs.push("phone " + e));
await m.goto(`${BASE}/q/${ids[3]}`); await m.waitForTimeout(4000);
await m.screenshot({ path: `${OUT}/qpage-phone.png`, fullPage: true });
console.log("rows:", await d.locator("#rows li").count(), "| label:", await d.textContent("#qLabel"),
  "| time:", await d.textContent("#qTime"), "| errors:", errs.length ? errs : "none");
clearInterval(keep);
await b.close();
