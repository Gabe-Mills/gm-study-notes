// Render the share images from share/card.html into PNGs at exact sizes.
// Usage: node share/render.mjs   (serves share/ on a local port so the shader file can load)
import { chromium } from "../tests/node_modules/playwright-core/index.mjs";
import http from "node:http";
import { readFile } from "node:fs/promises";
import path from "node:path";

const dir = path.dirname(new URL(import.meta.url).pathname), root = path.dirname(dir);
const types = { ".html": "text/html", ".frag": "text/plain", ".mjs": "text/javascript", ".png": "image/png" };
const server = http.createServer(async (req, res) => {
  const f = path.join(root, decodeURIComponent(new URL(req.url, "http://x").pathname));
  let body;
  try { body = await readFile(f); } catch { res.writeHead(404); res.end(); return; }
  res.writeHead(200, { "Content-Type": types[path.extname(f)] || "application/octet-stream" }); res.end(body);
}).listen(0);
const port = server.address().port;
const jobs = [
  ["og", 1200, 630, "../static/og.png"],
  ["post", 1080, 1350, "gm-study-notes-instagram-post.png"],
  ["story", 1080, 1920, "gm-study-notes-instagram-story.png"],
];
const b = await chromium.launch({ channel: "chrome", args: ["--use-angle=metal"] });
for (const [layout, w, h, out] of jobs) {
  const p = await b.newPage({ viewport: { width: w, height: h }, deviceScaleFactor: 1 });
  await p.goto(`http://127.0.0.1:${port}/share/card.html?layout=${layout}`);
  await p.waitForFunction(() => document.body.dataset.ready === "1", null, { timeout: 60000 });
  await p.waitForTimeout(400);
  await p.screenshot({ path: path.join(dir, out) });
  console.log("wrote", out, `${w}x${h}`);
  await p.close();
}
await b.close(); server.close();
