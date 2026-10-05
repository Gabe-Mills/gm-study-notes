// LOCAL preview of the rendering page and the ready page: one notes ticket, watched until it's done.
// Usage: OUT=dir BASE=http://127.0.0.1:5211 node render-preview.mjs
import { chromium } from "playwright-core";
const OUT = process.env.OUT, BASE = process.env.BASE || "http://127.0.0.1:5211";
const id = (await (await fetch(`${BASE}/api/queue`, { method: "POST", headers: { "Content-Type": "application/json", "cf-connecting-ip": "10.9.9.9" },
  body: JSON.stringify({ url: "https://youtu.be/jNQXAC9IVRw", kind: "notes" }) })).json()).id;
const b = await chromium.launch({ channel: "chrome", args: ["--autoplay-policy=no-user-gesture-required"] });
const errs = [];
const p = await b.newPage({ viewport: { width: 1440, height: 900 } });
p.on("pageerror", e => errs.push(String(e)));
await p.addInitScript(() => {  // count chimes: wrap the oscillator start
  window.__chimes = 0; const C = window.AudioContext; if (!C) return;
  const o = C.prototype.createOscillator; C.prototype.createOscillator = function () { window.__chimes++; return o.call(this); };
});
await p.goto(`${BASE}/q/${id}`);
await p.waitForFunction(() => document.getElementById("qpage").className === "mode-run", null, { timeout: 60000 });
await p.waitForTimeout(6000);
await p.screenshot({ path: `${OUT}/render-run.png` });
console.log("estimate shown as:", await p.textContent("#qTime"));
console.log("running:", await p.textContent("#qLabel"), "|", await p.textContent("#qTime"), "|", await p.textContent("#qSub"));
await p.reload();  // refresh in the middle: should come back to the same place
await p.waitForTimeout(3000);
console.log("after refresh:", new URL(p.url()).pathname === `/q/${id}`, "| mode:", await p.evaluate(() => document.getElementById("qpage").className));
await p.waitForFunction(() => document.getElementById("qpage").className === "mode-done", null, { timeout: 600000 });
await p.waitForTimeout(1500);
await p.screenshot({ path: `${OUT}/render-done.png` });
console.log("ready:", await p.textContent("#qLabel"), "|", await p.textContent("#qTime"), "| chime oscillators:", await p.evaluate(() => window.__chimes));
await p.click("#endNow"); console.log("first tap:", await p.textContent("#endNow"));
await p.click("#endNow"); await p.waitForTimeout(2500);
console.log("after second tap:", (await p.textContent("#tBody")).replace(/\s+/g, " ").trim().slice(0, 50), "| label:", await p.textContent("#qLabel"), "| errors:", errs.length ? errs : "none");
await b.close();
