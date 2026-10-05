// Capture the loader at several moments to confirm it varies (local server on :5211, a waiting ticket).
import { chromium } from "playwright-core";
const OUT = process.env.OUT, BASE = "http://127.0.0.1:5211";
for (let i = 0; i < 30; i++) { try { await fetch(`${BASE}/api/queue`); break; } catch { await new Promise(r => setTimeout(r, 1000)); } }
// A slow notes job goes first, so our ticket sits in line and the loader keeps moving.
await fetch(`${BASE}/api/queue`, { method: "POST", headers: { "Content-Type": "application/json", "cf-connecting-ip": "10.7.7.1" },
  body: JSON.stringify({ url: "https://youtu.be/jNQXAC9IVRw", kind: "notes" }) });
const id = (await (await fetch(`${BASE}/api/queue`, { method: "POST", headers: { "Content-Type": "application/json", "cf-connecting-ip": "10.7.7.7" },
  body: JSON.stringify({ url: "https://youtu.be/jNQXAC9IVRw", kind: "transcript" }) })).json()).id;
const b = await chromium.launch({ channel: "chrome" }); const errs = [];
const p = await b.newPage({ viewport: { width: 900, height: 700 } });
p.on("pageerror", e => errs.push(String(e)));
await p.goto(`${BASE}/q/${id}`);
for (let k = 0; k < 4; k++) { await p.waitForTimeout(4500); await p.locator("#loader").screenshot({ path: `${OUT}/loader-${k}.png` }); }
console.log("errors:", errs.length ? errs : "none");
await b.close();
