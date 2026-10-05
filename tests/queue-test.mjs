// Live queue test: A (notes) and B (transcript) join; B waits its turn; both download; A reviews.
// Usage: OUT=dir node queue-test.mjs
import { chromium } from "playwright-core";
const OUT = process.env.OUT, SITE = "https://youtubelink.gabemills.com/";
const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);
const b = await chromium.launch({ channel: "chrome" });
const open = async () => (await b.newContext({ viewport: { width: 1280, height: 1000 }, acceptDownloads: true })).newPage();
async function join(p, kind, link) {
  await p.goto(SITE);
  await p.check(`input[name=kind][value=${kind}]`, { force: true });
  await p.fill("#url", link);
  await p.click("#go");
  await p.waitForSelector("#ticket:not(.hidden)");
  await p.waitForTimeout(2500);
}
const status = async p => (await p.textContent("#tBody")).replace(/\s+/g, " ").trim().slice(0, 110);
const A = await open(), B = await open();
await join(A, "notes", "https://youtu.be/JqBH2qq52MA?si=3MRCoUD_hKYDwUjF");
log("A joined:", await status(A));
await join(B, "transcript", "https://youtu.be/jNQXAC9IVRw");
log("B joined:", await status(B));
await B.screenshot({ path: `${OUT}/qt-waiting.png` });
const t0 = Date.now(); let lastLog = 0, shot = false;
while (!(await A.isVisible("#gClock"))) {
  if (Date.now() - lastLog > 120e3) { log("A:", await status(A), "|| B:", await status(B)); lastLog = Date.now(); }
  if (!shot && Date.now() - t0 > 100e3) { await A.screenshot({ path: `${OUT}/qt-working.png` }); shot = true; }
  if (/Try again|Deleted|left the line/.test(await status(A))) { log("A FAILED:", await status(A)); process.exit(1); }
  await A.waitForTimeout(5000);
}
log(`A ready after ${Math.round((Date.now() - t0) / 1000)}s:`, await status(A));
await A.waitForTimeout(1500);
await A.screenshot({ path: `${OUT}/qt-ready.png` });
const [dl] = await Promise.all([A.waitForEvent("download"), A.click("#tBody a.btn-primary")]);
await dl.saveAs(`${OUT}/qt-notes.pdf`);
log("A downloaded:", dl.suggestedFilename());
// review
await A.click('#starsIn button[data-s="5"]');
await A.fill("#rvName", "Site test"); await A.fill("#rvText", "Automated check, will be removed.");
await A.click("#rvSend"); await A.waitForTimeout(2500);
log("review wall:", (await A.textContent("#wall")).replace(/\s+/g, " ").trim().slice(0, 90), "| avg:", await A.textContent("#avgNum"));
// B should now be working or done
await B.waitForSelector("#gClock", { timeout: 300e3 });
log("B ready:", await status(B));
const [dlb] = await Promise.all([B.waitForEvent("download"), B.click("#tBody a.btn-primary")]);
await dlb.saveAs(`${OUT}/qt-transcript.txt`);
log("B downloaded:", dlb.suggestedFilename());
// wait out A's 3 minutes and check the file is gone
const pdfUrl = await A.getAttribute("#tBody a.btn-primary", "href");
await A.waitForFunction(() => /Deleted/.test(document.getElementById("tBody").textContent), null, { timeout: 240e3 });
const r = await A.request.get(new URL(pdfUrl, SITE).href, { headers: { "User-Agent": "Mozilla/5.0 Chrome/141.0" } });
log("A after 3 minutes:", await status(A), "| file status:", r.status());
await A.screenshot({ path: `${OUT}/qt-deleted.png` });
await b.close();
