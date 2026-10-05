// Screenshot only the home hero (for tuning the fog), from disk.
import { chromium } from "playwright-core";
const OUT = process.env.OUT, NAME = process.env.NAME || "fog", SRC = "file://" + new URL("../static/index.html", import.meta.url).pathname;
const b = await chromium.launch({ channel: "chrome" });
const d = await b.newPage({ viewport: { width: 1440, height: 900 } });
await d.goto(SRC); await d.waitForTimeout(3500);
await d.screenshot({ path: `${OUT}/${NAME}.png` });
await b.close();
