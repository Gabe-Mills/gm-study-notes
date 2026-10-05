"""GM Study Notes: a one-at-a-time queue that turns YouTube videos into transcripts or study notes.

People paste a link, pick Transcript or Notes, and join the queue. A single worker
handles one ticket at a time (notes use every CPU core). Finished files sit in a
per-ticket folder for three minutes and are then deleted. Nothing else is kept
except the reviews people leave and the step timings used for wait estimates.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import notes as rich

ROOT = Path(__file__).parent
DATA = ROOT / "data"
TICKETS = DATA / "tickets"
TIMINGS = DATA / "timings.json"
REVIEWS = DATA / "reviews.json"

# ffmpeg lives in ~/.local/bin on this machine; make sure child processes see it.
os.environ["PATH"] = f"{Path.home() / '.local/bin'}:/opt/homebrew/bin:{os.environ.get('PATH', '')}"

WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "mlx-community/whisper-large-v3-turbo")
# Local notes model in Ollama; empty means notes come from headless Claude (Mac).
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "")

GRAB_SECONDS = 180       # how long finished files stay downloadable
ABANDON_SECONDS = 600    # closed the tab? your place is kept this long, so reopening the site puts you back in line
FORGET_SECONDS = 3600    # finished tickets are forgotten after an hour (reviews still need them briefly)
MAX_WAITING = 30
PER_PERSON = 2           # active tickets per visitor
CHARS_PER_MIN = 950      # spoken English, for guessing notes time before the words are fetched
DEFAULT_LISTEN = {"download": 15.0, "listen_per_min": 15.0}


def purge_stored_docs():
    """The site keeps no transcripts or notes: clear anything left from earlier versions or a restart."""
    DATA.mkdir(exist_ok=True)
    for p in DATA.iterdir():
        if p.name in (TIMINGS.name, REVIEWS.name):
            continue
        shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
    TICKETS.mkdir()


purge_stored_docs()
app = FastAPI(title="GM Study Notes")
tickets: dict[str, dict] = {}
queue: list[str] = []
lock = threading.RLock()
wake = threading.Event()


# ---------------------------------------------------------------- video helpers

ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def video_id(url: str) -> str:
    url = url.strip()
    if ID_RE.match(url):
        return url
    if not re.match(r"^https?://", url):
        url = "https://" + url
    u = urllib.parse.urlparse(url)
    host = u.netloc.lower().removeprefix("www.").removeprefix("m.").removeprefix("music.")
    if host == "youtu.be":
        vid = u.path.strip("/").split("/")[0]
    elif host.endswith("youtube.com") or host.endswith("youtube-nocookie.com"):
        q = urllib.parse.parse_qs(u.query)
        if "v" in q:
            vid = q["v"][0]
        else:
            parts = [p for p in u.path.split("/") if p]
            vid = parts[1] if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live", "v", "e"} else ""
    else:
        vid = ""
    if not ID_RE.match(vid):
        raise ValueError("That doesn't look like a YouTube video link.")
    return vid


def video_meta(vid: str) -> dict:
    meta = {"title": vid, "channel": "", "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"}
    try:
        q = urllib.parse.urlencode({"url": f"https://www.youtube.com/watch?v={vid}", "format": "json"})
        with urllib.request.urlopen(f"https://www.youtube.com/oembed?{q}", timeout=8) as r:
            d = json.load(r)
        meta.update(title=d.get("title", vid), channel=d.get("author_name", ""))
    except Exception:
        pass
    return meta


def video_length(vid: str) -> float | None:
    """Seconds, for wait estimates. Best effort: YouTube sometimes refuses."""
    try:
        import yt_dlp

        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True, "socket_timeout": 10}) as ydl:
            return float(ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False).get("duration") or 0) or None
    except Exception:
        return None


def fetch_captions(vid: str) -> tuple[list[dict], str] | None:
    """Return (segments, language) from YouTube captions, or None if there are none."""
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api._errors import NoTranscriptFound, TranscriptsDisabled

    try:
        listing = list(YouTubeTranscriptApi().list(vid))
    except (TranscriptsDisabled, NoTranscriptFound):
        return None
    if not listing:
        return None
    # Prefer human captions, then English, then whatever exists.
    listing.sort(key=lambda t: (t.is_generated, not t.language_code.startswith("en")))
    pick = listing[0]
    segs = [{"start": s.start, "end": s.start + s.duration, "text": s.text} for s in pick.fetch()]
    return segs, pick.language  # language already says "(auto-generated)" for auto captions


_fw_model = None


def run_whisper(path: str) -> tuple[list[dict], str]:
    """mlx-whisper on Apple Silicon, faster-whisper (CPU int8) everywhere else."""
    if sys.platform == "darwin":
        import mlx_whisper

        out = mlx_whisper.transcribe(path, path_or_hf_repo=WHISPER_MODEL)
        segs = [{"start": s["start"], "end": s["end"], "text": s["text"].strip()} for s in out.get("segments", [])]
        return segs, out.get("language", "unknown")

    global _fw_model
    from faster_whisper import WhisperModel
    import numpy as np

    if _fw_model is None:
        _fw_model = WhisperModel(os.environ.get("WHISPER_MODEL_CPU", "small"), device="cpu", compute_type="int8")
    # Decode with ffmpeg ourselves: faster-whisper's PyAV path breaks on newer PyAV releases.
    pcm = subprocess.run(["ffmpeg", "-nostdin", "-i", path, "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
                         capture_output=True, check=True).stdout
    audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
    it, info = _fw_model.transcribe(audio, vad_filter=True)
    return [{"start": s.start, "end": s.end, "text": s.text.strip()} for s in it], info.language


def listen_to(vid: str, set_stage) -> tuple[list[dict], str]:
    import yt_dlp

    tm = timings()
    tmp = Path(tempfile.mkdtemp(prefix="ytnotes-"))
    try:
        set_stage("Downloading the sound", tm["download"], 0.02)
        t0 = time.time()
        opts = {"format": "bestaudio/best", "outtmpl": str(tmp / "audio.%(ext)s"), "quiet": True, "no_warnings": True, "noplaylist": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=True)
        minutes = (info.get("duration") or 600) / 60
        measured = {"download": time.time() - t0}
        set_stage("Listening to the video", tm["listen_per_min"] * minutes, 0.05)
        t0 = time.time()
        segs, lang = run_whisper(str(next(tmp.glob("audio.*"))))
        measured["listen_per_min"] = (time.time() - t0) / max(minutes, 0.1)
        learn(measured)
        return [s for s in segs if s["text"]], lang
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def stamp(t: float) -> str:
    t = int(t)
    h, m, s = t // 3600, t % 3600 // 60, t % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def friendly(e: Exception) -> str:
    msg = re.sub(r"^ERROR:\s*(\[\w+\]\s*[\w-]+:\s*)?", "", str(e) or e.__class__.__name__)
    low = msg.lower()
    if "private video" in low:
        return "This video is private."
    if "unavailable" in low or "videounavailable" in e.__class__.__name__.lower():
        return "This video is unavailable."
    if "sign in to confirm" in low or ("age" in low and "restricted" in low):
        return "YouTube needs a sign-in for this video, so we can't get it."
    if "requestblocked" in e.__class__.__name__.lower() or "ipblocked" in e.__class__.__name__.lower():
        return "YouTube is blocking us right now. Try again with Listen turned on."
    return msg.splitlines()[0][:300]


# ---------------------------------------------------------------- timings and estimates

def timings() -> dict:
    try:
        return {**rich.DEFAULT_TIMINGS, **DEFAULT_LISTEN, **json.loads(TIMINGS.read_text())}
    except Exception:
        return {**rich.DEFAULT_TIMINGS, **DEFAULT_LISTEN}


def learn(measured: dict):
    tm = timings()
    for k, v in measured.items():
        if v and v > 0:
            tm[k] = round(0.5 * tm.get(k, v) + 0.5 * v, 2)
    TIMINGS.write_text(json.dumps(tm))


def estimate(t: dict) -> float:
    """Seconds a ticket needs once it starts, guessed from the video's length."""
    tm = timings()
    minutes = (t.get("duration") or 600) / 60
    words = tm["download"] + tm["listen_per_min"] * minutes if t["listen"] else 8
    if t["kind"] == "transcript":
        return words
    return words + tm["profile"] + tm["section_per_kchar"] * minutes * CHARS_PER_MIN / 1000 + tm["overview"] + tm["render"]


def remaining(t: dict) -> float:
    if t["status"] != "running":
        return estimate(t)
    if t.get("left") is None:
        return max(estimate(t) * (1 - (t.get("pct") or 0)) - (time.time() - t["stage_at"]), 5)
    return max(t["left"] - (time.time() - t["stage_at"]), 5)


def running() -> dict | None:
    return next((t for t in tickets.values() if t["status"] == "running"), None)


# ---------------------------------------------------------------- the worker

def worker():
    while True:
        with lock:
            tid = queue.pop(0) if queue else None
            t = tickets.get(tid) if tid else None
            if t:
                t.update(status="running", stage="Starting", stage_at=time.time(), left=None, pct=0)
        if not t:
            wake.wait(2)
            wake.clear()
            continue
        try:
            process(t)
        except Exception as e:
            shutil.rmtree(TICKETS / t["id"], ignore_errors=True)
            t.update(status="error", error=friendly(e), files={}, ready_at=time.time())


def process(t: dict):
    folder = TICKETS / t["id"]
    folder.mkdir(exist_ok=True)

    def set_stage(text, left=None, pct=None):
        t.update(stage=text, left=left, pct=pct, stage_at=time.time())

    # 1. The words: subtitles if there are any, otherwise listen.
    segs, lang, source = None, "", "captions"
    if not t["listen"]:
        set_stage("Getting the words", 8, 0.01)
        try:
            caps = fetch_captions(t["vid"])
            if caps:
                segs, lang = caps
        except Exception:
            pass  # blocked or broken subtitles: listen instead
    if not segs:
        segs, lang = listen_to(t["vid"], set_stage)
        source = "whisper"
    if not segs:
        raise RuntimeError("No speech was found in this video.")
    t["duration"] = segs[-1]["end"]
    name = re.sub(r"[^\w\- ]+", "", t["title"]).strip()[:80] or t["vid"]
    link = f"https://www.youtube.com/watch?v={t['vid']}"

    if t["kind"] == "transcript":
        lines = "\n".join(f"[{stamp(s['start'])}] {s['text']}" for s in segs)
        (folder / "transcript.txt").write_text(f"{t['title']}\n{link}\n\n{lines}\n")
        files = {"transcript.txt": ("Transcript", f"{name} - transcript.txt")}
    else:
        # 2. The notes, laid out as a PDF.
        video = {"id": t["vid"], "title": t["title"], "channel": t["channel"], "thumbnail": t["thumbnail"],
                 "source": source, "language": lang, "segments": segs}
        nb = rich.build_notes(video, OLLAMA_MODEL, set_stage, timings())
        t0 = time.time()
        files = {}
        # The same notes in three looks; the person picks one when they download.
        for theme, label in (("color", "Multi-color"), ("red", "Red"), ("bw", "Black & white")):
            page = rich.render_html(nb, theme)
            if theme == "color":
                (folder / "notes.html").write_text(page)
            if rich.render_pdf(page, folder / f"notes-{theme}.pdf"):
                files[f"notes-{theme}.pdf"] = (label, f"{name} - notes ({label.lower()}).pdf")
        (folder / "notes.txt").write_text(f"{t['title']}\n{link}\n\n{rich.render_markdown(nb)}\n")
        if OLLAMA_MODEL:  # only learn from the server's own model
            learn({**nb.get("timing", {}), "render": time.time() - t0})
        files.update({"notes.html": ("View", f"{name} - notes.html"), "notes.txt": ("Text", f"{name} - notes.txt")})
        t["subject"] = nb.get("profile", {}).get("subject")
    now = time.time()
    t.update(status="ready", files=files, ready_at=now, expires_at=now + GRAB_SECONDS, stage="Done", pct=1, left=0)


def janitor():
    """Delete files when their 3 minutes are up, drop abandoned places in line, forget old tickets."""
    while True:
        time.sleep(3)
        now = time.time()
        with lock:
            for tid, t in list(tickets.items()):
                if t["status"] == "ready" and now >= t["expires_at"]:
                    shutil.rmtree(TICKETS / tid, ignore_errors=True)
                    t.update(status="expired", files={})
                elif t["status"] == "waiting" and now - t["last_seen"] > ABANDON_SECONDS:
                    t["status"] = "gone"
                    if tid in queue:
                        queue.remove(tid)
                elif t["status"] in ("expired", "gone", "error") and now - (t.get("ready_at") or t["created"]) > FORGET_SECONDS:
                    del tickets[tid]


threading.Thread(target=worker, daemon=True).start()
threading.Thread(target=janitor, daemon=True).start()


# ---------------------------------------------------------------- API

def visitor(request: Request) -> str:
    # Behind the Cloudflare tunnel every request comes from 127.0.0.1; the real address is in this header.
    return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")


def public(t: dict) -> dict:
    now = time.time()
    with lock:
        pos = queue.index(t["id"]) + 1 if t["id"] in queue else 0
        cur = running()
        ahead = (remaining(cur) if cur else 0) + sum(estimate(tickets[q]) for q in queue[:max(pos - 1, 0)] if q in tickets)
        in_line = len(queue) + (1 if cur else 0)
    out = {k: t.get(k) for k in ("id", "kind", "title", "channel", "thumbnail", "duration", "status", "stage", "pct", "error", "reviewed", "subject")}
    out["alias"] = alias(t["id"])
    out.update(position=pos, in_line=in_line, working=bool(cur), wait=round(ahead) if t["status"] == "waiting" else None,
               needs=round(estimate(t)), left=round(remaining(t)) if t["status"] == "running" else None)
    if t["status"] == "ready":
        out["expires_in"] = max(0, round(t["expires_at"] - now))
        out["files"] = [{"name": n, "label": lbl, "url": f"/api/ticket/{t['id']}/file/{n}"} for n, (lbl, _) in t["files"].items()]
    return out


class JoinReq(BaseModel):
    url: str
    kind: str = "notes"
    listen: bool = False


@app.post("/api/queue")
def join(req: JoinReq, request: Request):
    try:
        vid = video_id(req.url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if req.kind not in ("notes", "transcript"):
        raise HTTPException(400, "Pick Notes or Transcript.")
    who = visitor(request)
    with lock:
        mine = [t for t in tickets.values() if t["who"] == who and t["status"] in ("waiting", "running")]
        if len(mine) >= PER_PERSON:
            raise HTTPException(429, f"You already have {len(mine)} in the queue. Wait for one to finish.")
        if len(queue) >= MAX_WAITING:
            raise HTTPException(503, "The queue is full right now. Try again in a few minutes.")
        now = time.time()
        t = {"id": uuid.uuid4().hex, "vid": vid, "kind": req.kind, "listen": req.listen, "who": who,
             "title": vid, "channel": "", "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg", "duration": None,
             "status": "waiting", "created": now, "last_seen": now, "stage": "", "pct": 0, "left": None,
             "stage_at": now, "files": {}, "reviewed": False}
        tickets[t["id"]] = t
        queue.append(t["id"])

    def peek():  # title and length for the waiting screen and the estimates
        t.update(video_meta(vid))
        if t["duration"] is None:
            t["duration"] = video_length(vid)
    threading.Thread(target=peek, daemon=True).start()
    wake.set()
    return public(t)


# Other people show up on the leaderboard under a made-up name, never their video.
ADJ = ["Ember", "Crimson", "Velvet", "Night", "Ash", "Scarlet", "Cinder", "Ruby", "Smoke", "Rust", "Garnet", "Flame", "Ink", "Dusk"]
ANIMAL = ["Fox", "Owl", "Lynx", "Otter", "Raven", "Koi", "Hare", "Wolf", "Moth", "Crane", "Bear", "Hawk", "Heron", "Wren"]


def alias(tid: str) -> str:
    return f"{ADJ[int(tid[:6], 16) % len(ADJ)]} {ANIMAL[int(tid[6:12], 16) % len(ANIMAL)]}"


def get_ticket(tid: str) -> dict:
    t = tickets.get(tid)
    if not t:
        raise HTTPException(404, "This place in line no longer exists.")
    return t


@app.get("/api/ticket/{tid}")
def ticket(tid: str):
    t = get_ticket(tid)
    t["last_seen"] = time.time()
    return public(t)


@app.delete("/api/ticket/{tid}")
def leave(tid: str):
    """Leave the line, or delete finished files early."""
    t = get_ticket(tid)
    with lock:
        if tid in queue:
            queue.remove(tid)
            t["status"] = "gone"
        elif t["status"] == "ready":
            shutil.rmtree(TICKETS / tid, ignore_errors=True)
            t.update(status="expired", files={})
    return {"ok": True}


@app.get("/api/ticket/{tid}/file/{name}")
def file(tid: str, name: str, download: int = 1):
    t = get_ticket(tid)
    if t["status"] != "ready" or name not in t["files"]:
        raise HTTPException(410, "This file was deleted. Files are only kept for 3 minutes.")
    path = TICKETS / tid / name
    if not path.exists():
        raise HTTPException(410, "This file was deleted.")
    kinds = {".pdf": "application/pdf", ".txt": "text/plain; charset=utf-8", ".html": "text/html; charset=utf-8"}
    return FileResponse(path, media_type=kinds[path.suffix], filename=t["files"][name][1] if download else None,
                        content_disposition_type="attachment" if download else "inline")


@app.get("/api/queue")
def queue_info():
    with lock:
        cur = running()
        wait = (remaining(cur) if cur else 0) + sum(estimate(tickets[q]) for q in queue if q in tickets)
        return {"waiting": len(queue), "working": bool(cur), "in_line": len(queue) + (1 if cur else 0), "wait": round(wait)}


@app.get("/api/line/{tid}")
def line(tid: str):
    """The whole line for the queue page: who's being worked on, then everyone waiting, with start times."""
    t = get_ticket(tid)
    t["last_seen"] = time.time()
    rows, clock = [], 0.0
    with lock:
        cur = running()
        if cur:
            clock = remaining(cur)
            rows.append({"place": 0, "name": alias(cur["id"]), "kind": cur["kind"], "length": cur.get("duration"),
                         "state": "working", "pct": cur.get("pct") or 0, "starts_in": 0, "done_in": round(clock), "me": cur["id"] == tid})
        for i, q in enumerate(queue, 1):
            x = tickets.get(q)
            if not x:
                continue
            need = estimate(x)
            rows.append({"place": i, "name": alias(q), "kind": x["kind"], "length": x.get("duration"), "state": "waiting",
                         "starts_in": round(clock), "done_in": round(clock + need), "me": q == tid})
            clock += need
    return {"rows": rows[:60], "total": len(rows)}


# ---------------------------------------------------------------- reviews

class ReviewReq(BaseModel):
    ticket: str
    name: str
    stars: int
    comment: str = ""


def read_reviews() -> list[dict]:
    try:
        return json.loads(REVIEWS.read_text())
    except Exception:
        return []


@app.post("/api/reviews")
def add_review(req: ReviewReq):
    t = get_ticket(req.ticket)
    if t["status"] not in ("ready", "expired") or t.get("reviewed"):
        raise HTTPException(400, "You can leave one review after your file is ready.")
    name = re.sub(r"\s+", " ", req.name).strip()[:40]
    comment = re.sub(r"\s+", " ", req.comment).strip()[:400]
    if not name or not 1 <= req.stars <= 5:
        raise HTTPException(400, "Add your name and 1 to 5 stars.")
    with lock:
        items = read_reviews()
        items.append({"name": name, "stars": req.stars, "comment": comment, "kind": t["kind"], "at": int(time.time())})
        REVIEWS.write_text(json.dumps(items[-500:]))
        t["reviewed"] = True
    return {"ok": True}


@app.get("/api/reviews")
def reviews():
    items = read_reviews()
    avg = round(sum(r["stars"] for r in items) / len(items), 1) if items else None
    dist = {str(n): sum(1 for r in items if r["stars"] == n) for n in range(1, 6)}
    return {"count": len(items), "avg": avg, "dist": dist, "items": list(reversed(items))[:12]}


@app.get("/")
@app.get("/q/{tid}")
def index(tid: str = ""):
    return FileResponse(ROOT / "static/index.html")


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
