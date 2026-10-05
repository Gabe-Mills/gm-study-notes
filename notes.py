"""College-style study notes: transcript → profile → sections → Cornell notes → HTML + PDF.

First pass works out what kind of video it is (subject, discipline, format), which
decides the extra components each section gets: formulas and worked examples for
STEM, timelines for history, claim/evidence for humanities, case briefs for law,
vocabulary for languages, and so on. Every section is laid out Cornell-style (cue
questions | notes, then a summary), and the end matter is for studying: a formula
sheet, timeline, glossary, exam focus, and a self-test with a separate answer key.
Numbers and quotes are checked against the transcript before they reach the page.
"""

from __future__ import annotations

import base64
import datetime as dt
import html
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
# Characters of transcript per section; ~4 chars per token keeps each call small enough for CPU.
SECTION_CHARS = int(os.environ.get("SECTION_CHARS", "12000"))
ACCENT = "#e5322d"


# ---------------------------------------------------------------- model calls

def _close_json(text: str) -> str:
    """Finish JSON that was cut off mid-way: cut back to the last complete value and close what is open."""
    stack, in_str, esc, cut, cut_stack = [], False, False, 0, []
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if stack:
                stack.pop()
            cut, cut_stack = i + 1, list(stack)
        elif ch == ",":
            cut, cut_stack = i, list(stack)
    return text[:cut].rstrip().rstrip(",") + "".join(reversed(cut_stack))


def _parse(text: str) -> dict | None:
    start = text.find("{")
    if start < 0:
        return None
    chunk = text[start:]
    for candidate in (chunk[:chunk.rfind("}") + 1], _close_json(chunk)):
        try:
            out = json.loads(candidate)
            if isinstance(out, dict):
                return out
        except Exception:
            pass
    return None


def _ollama(prompt: str, schema: dict, model: str, num_predict: int, temperature: float) -> tuple[str, str]:
    body = {
        "model": model, "stream": False, "format": schema,
        "messages": [{"role": "user", "content": prompt}],
        # num_predict caps output so a model stuck repeating itself can't run forever.
        "options": {"num_ctx": 16384, "num_predict": num_predict, "temperature": temperature},
    }
    if "qwen" in model.lower():
        body["think"] = False
    req = urllib.request.Request(f"{OLLAMA_URL}/api/chat", json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=3600) as r:
        d = json.load(r)
    return d["message"].get("content") or "", d.get("done_reason", "")


def llm_json(prompt: str, schema: dict, model: str, budget: int = 4096) -> dict:
    """One structured call. Ollama when a local model is set, otherwise headless Claude.
    Local models occasionally return an empty or cut-off reply, so retry once (with more
    room if it ran out), and repair nearly-complete JSON."""
    if model:
        text, why = _ollama(prompt, schema, model, budget, 0.3)
        out = _parse(text)  # repaired if it ran out of room; a usable answer beats a slow retry
        if out is not None:
            return out
        # Unreadable: try once more, with extra room only if it was cut off.
        text, why = _ollama(prompt, schema, model, budget * 2 if why == "length" else budget, 0.5)
        out = _parse(text)
        if out is not None:
            return out
        # Log the shape only: the site keeps no copies of what people put through it.
        print(f"notes model: unreadable reply twice ({len(text)} chars, ended: {why})", file=sys.stderr, flush=True)
        raise RuntimeError("The notes model gave an unreadable answer twice.")
    else:
        claude = shutil.which("claude") or str(Path.home() / "bin/claude")
        full = f"{prompt}\n\nReply with only a JSON object matching this JSON Schema, no prose:\n{json.dumps(schema)}"
        r = subprocess.run([claude, "-p", "--model", "sonnet", "--output-format", "text"], input=full,
                           capture_output=True, text=True, timeout=900, cwd=tempfile.gettempdir())
        if r.returncode != 0:
            raise RuntimeError(f"Claude failed: {(r.stderr or r.stdout).strip()[:300]}")
        text = r.stdout
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise RuntimeError("The model did not return notes in the expected format.")
    return json.loads(m.group(0))


# ---------------------------------------------------------------- schemas

S = {"type": "string"}
SL = {"type": "array", "items": S, "maxItems": 8}


def obj(**props) -> dict:
    return {"type": "object", "properties": props, "required": list(props)}


def arr(item: dict, most: int = 10) -> dict:
    # maxItems goes into the decoding grammar, so a model can't loop on a list forever.
    return {"type": "array", "items": item, "maxItems": most}


DISCIPLINES = {
    "math": "Mathematics / statistics",
    "science": "Natural science",
    "computing": "Computer science / programming",
    "engineering": "Engineering",
    "health": "Health / medicine / nursing",
    "history": "History",
    "humanities": "Humanities (literature, English, writing, philosophy, religion)",
    "social_science": "Social science (psychology, sociology, politics, economics)",
    "business": "Business / finance / marketing",
    "law": "Law",
    "language": "Learning a foreign or second language",
    "study_skills": "Exam prep / study skills / test strategy",
    "arts": "Arts / music / design / film",
    "practical": "Practical skill / how-to",
    "commentary": "News / commentary / discussion",
    "other": "General",
}

# What each kind of course needs on the page, beyond the Cornell basics every section gets.
COMPONENTS_FOR = {
    "math": ["formulas", "examples", "mistakes"],
    "science": ["formulas", "processes", "examples", "mistakes"],
    "computing": ["code", "processes", "examples", "mistakes"],
    "engineering": ["formulas", "examples", "processes", "mistakes"],
    "health": ["processes", "comparisons", "mistakes"],
    "history": ["events", "people", "causes"],
    "humanities": ["arguments", "people"],
    "social_science": ["arguments", "comparisons", "people"],
    "business": ["frameworks", "comparisons", "examples"],
    "law": ["cases", "arguments"],
    "language": ["vocab", "examples", "mistakes"],
    "study_skills": ["processes", "comparisons", "mistakes"],
    "arts": ["people", "comparisons", "processes"],
    "practical": ["processes", "mistakes", "comparisons"],
    "commentary": ["arguments", "people", "comparisons"],
    "other": ["comparisons", "processes"],
}

COMPONENT_SCHEMA = {
    "formulas": arr(obj(name=S, latex=S, meaning=S, variables=S), 8),
    "examples": arr(obj(problem=S, steps=arr(S, 12), answer=S), 4),
    "mistakes": SL,
    "processes": arr(obj(name=S, steps=arr(S, 12)), 4),
    "code": arr(obj(language=S, code=S, explanation=S), 4),
    "events": arr(obj(date=S, event=S)),
    "people": arr(obj(name=S, role=S)),
    "causes": arr(obj(cause=S, effect=S)),
    "arguments": arr(obj(claim=S, evidence=S, counter=S)),
    "frameworks": arr(obj(name=S, parts=SL, use=S)),
    "comparisons": arr(obj(title=S, columns=arr(S, 5), rows=arr(arr(S, 5), 10)), 3),
    "cases": arr(obj(name=S, facts=S, issue=S, rule=S, holding=S, reasoning=S), 3),
    "vocab": arr(obj(term=S, meaning=S, example=S)),
}

COMPONENT_HELP = {
    "formulas": "formulas: every equation or formula stated or written, as LaTeX math (no $ signs, no \\text, use \\mathrm), with its name, what it means in plain words, and what each variable is.",
    "examples": "examples: worked examples or problems from the video, with the problem, every step in order, and the answer. Do not skip steps.",
    "mistakes": "mistakes: common errors, misconceptions or warnings the speaker points out.",
    "processes": "processes: any procedure, mechanism, algorithm or method as ordered steps.",
    "code": "code: short code shown or dictated, with language and a one-line explanation. Only real code from the video.",
    "events": "events: dated events (keep the date as said: year, decade, century) and what happened.",
    "people": "people: important people, groups or organizations and their role.",
    "causes": "causes: cause and effect pairs the speaker explains.",
    "arguments": "arguments: claims made, the evidence or reasoning given, and any counterpoint mentioned (empty string if none).",
    "frameworks": "frameworks: named models or frameworks, their parts, and when to use them.",
    "comparisons": "comparisons: when things are compared, a small table (title, column headers, rows of cells).",
    "cases": "cases: legal cases discussed as briefs: facts, issue, rule, holding, reasoning.",
    "vocab": "vocab: words or phrases taught, their meaning, and an example sentence.",
}

PROFILE_SCHEMA = obj(
    subject=S, discipline={"type": "string", "enum": list(DISCIPLINES)},
    format={"type": "string", "enum": ["lecture", "tutorial", "discussion", "interview", "news", "review", "documentary", "other"]},
    level={"type": "string", "enum": ["intro", "intermediate", "advanced", "general audience"]},
    extra_components=arr({"type": "string", "enum": list(COMPONENT_SCHEMA)}, 3),
)


def section_schema(components: list[str]) -> dict:
    point = obj(text=S, t=S, sub=arr(S, 3))
    base = dict(
        title=S, summary=S, cues=arr(S, 5), points=arr(point, 9),
        terms=arr(obj(term=S, definition=S), 10),
        numbers=arr(obj(label=S, value={"type": "number"}, unit=S, t=S, evidence=S), 15),
        quotes=arr(obj(text=S, t=S), 2),
    )
    base.update({c: COMPONENT_SCHEMA[c] for c in components})
    return obj(**base)


OVERVIEW_SCHEMA = obj(
    headline=S, tldr=S, summary=S, takeaways=arr(S, 7), exam_focus=arr(S, 6), action_items=arr(S, 6),
    review=arr(obj(q=S, a=S, kind={"type": "string", "enum": ["recall", "explain", "apply"]}), 12),
    charts=arr(obj(type={"type": "string", "enum": ["bar", "line", "pie"]}, title=S,
                   number_ids=arr({"type": "integer"}, 8), caption=S), 3),
)

PROFILE_PROMPT = """Classify this YouTube video so study notes can be tailored to it.
Title: "{title}"{channel}

- subject: the specific course-style subject, e.g. "Organic Chemistry", "US History", "Intro to Python", "AI industry news".
- discipline: the closest discipline. "language" is ONLY for learning a foreign or second language; a class about English or literature for native speakers is "humanities". Videos mainly about how to pass an exam, exam technique or study habits are "study_skills".
- format, level: as they fit.
- extra_components: any extra note components this video clearly needs beyond the usual ones for its discipline (often empty).

Opening of the transcript:
{text}
"""

SECTION_PROMPT = """You are an excellent college student writing Cornell-style study notes for part {n} of {total} of a video.
Video: "{title}"{channel}. Subject: {subject} ({discipline}, {level} {format}). This part covers {start} to {end}.

Return:
- title: a short, specific topic title for this part (max 8 words).
- summary: 2-4 sentences in your own words on what this part teaches or argues.
- cues: 3-5 questions for the Cornell cue column, the kind a professor would ask, each answered by the notes below.
- points: the 4-9 most important ideas in outline form. text = one clear sentence with the key term in **double asterisks**; sub = 0-3 short supporting details, examples or reasons that add NEW information (never restate the point); t = the [m:ss] timestamp where it comes up.
- terms: jargon, concepts, names a student must know, with a one-line definition from context.
- numbers: every concrete statistic, price, percentage, count, year or measurement stated. value is the number only, unit like "%", "$", "users", "GB", "years". evidence MUST be copied word for word from the transcript and contain the number.
- quotes: up to 2 memorable, self-contained lines of at least 8 words, copied from the transcript. You may drop filler words and fix obvious caption misspellings, nothing else.
{extra}
Rules: only use what is in this transcript. Fix obvious caption errors silently. Never invent facts, numbers, formulas or steps. Leave any list empty when the video has nothing for it.

Transcript:
{text}
"""

OVERVIEW_PROMPT = """You are finishing college study notes for the video "{title}"{channel} ({duration}).
Subject: {subject} ({discipline}, {level} {format}).
Below are the notes for each part, then a numbered list of verified numbers from the video.

Return:
- headline: the single main idea of the video as one sentence.
- tldr: 3-4 sentences a busy student can trust.
- summary: the Cornell summary for the whole video, 5-8 sentences that connect the parts in your own words.
- takeaways: 4-7 lessons or conclusions, one sentence each, key term in **double asterisks**.
- exam_focus: 3-6 things most likely to be tested or most important to remember, as short statements.
- review: 6-12 self-test questions with short correct answers drawn from the notes; mix recall (facts), explain (why/how) and apply (use it on a new case).
- action_items: concrete things the viewer is told or encouraged to do. Empty if none.
- charts: 0-3 charts, ONLY when several verified numbers can be compared meaningfully (same unit, same kind of thing). Use number_ids from the list. pie only for shares of a whole in %. line only for values over time. Empty if nothing is worth charting.

Notes by part:
{parts}

Verified numbers:
{numbers}
"""


# ---------------------------------------------------------------- helpers

def stamp(t: float) -> str:
    t = int(t)
    h, m, s = t // 3600, t % 3600 // 60, t % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_stamp(s: str, lo: float, hi: float) -> float:
    try:
        parts = [int(p) for p in re.findall(r"\d+", s)][-3:]
        sec = 0
        for p in parts:
            sec = sec * 60 + p
        return sec if lo - 5 <= sec <= hi + 5 else lo
    except Exception:
        return lo


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9%$.]+", " ", s.lower()).strip()


def said(quote: str, src: str) -> bool:
    """A quote counts if it is substantial and nearly every word pair in it occurs in the transcript.
    That allows dropped fillers ("um", repeated words) but not invented wording."""
    words = norm(quote).split()
    if len(words) < 7:
        return False
    sw = src.split()
    have = set(zip(sw, sw[1:]))
    pairs = list(zip(words, words[1:]))
    return sum(p in have for p in pairs) / len(pairs) >= 0.85


def adds(sub: str, point: str) -> bool:
    """A sub-point earns its place only if most of its words are not already in the point."""
    words = [w for w in norm(sub).split() if len(w) > 2]
    if not words:
        return False
    have = set(norm(point).split())
    return sum(w not in have for w in words) / len(words) >= 0.4


def split_sections(segs: list[dict], target: int = SECTION_CHARS) -> list[list[dict]]:
    """Cut at the longest pause near each size limit, so sections end between thoughts."""
    out, st, size = [], 0, 0
    for i, s in enumerate(segs):
        size += len(s["text"]) + 10
        if size >= target and i - st > 10:
            window = range(max(st + 5, i - 30), i + 1)
            cut = max(window, key=lambda j: segs[j]["start"] - segs[j - 1]["end"])
            out.append(segs[st:cut])
            st = cut
            size = sum(len(x["text"]) + 10 for x in segs[st:i + 1])
    tail = segs[st:]
    if out and sum(len(x["text"]) for x in tail) < target * 0.3:
        out[-1] = out[-1] + tail
    elif tail:
        out.append(tail)
    return out


def md_inline(s: str) -> str:
    """Escape model text, then allow **bold** only."""
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html.escape(s or ""))


def _clean_list(items, *keys):
    """Drop entries the model left empty."""
    out = []
    for x in items or []:
        if isinstance(x, str):
            if x.strip():
                out.append(x.strip())
        elif isinstance(x, dict) and all(str(x.get(k, "")).strip() for k in keys):
            out.append(x)
    return out


# ---------------------------------------------------------------- pipeline

# Seconds per step on the 8-core VM with Qwen3.6-35B-A3B, measured 2026-10-05. The app keeps
# these up to date from real runs (data/timings.json), so estimates improve with use.
DEFAULT_TIMINGS = {"profile": 70.0, "section_per_kchar": 30.0, "overview": 160.0, "render": 20.0}


def plan(t: dict, timings: dict | None = None) -> tuple[list[list[dict]], list[float]]:
    """Sections and the estimated seconds for every step: subject, each section, summary, PDF."""
    tm = {**DEFAULT_TIMINGS, **(timings or {})}
    sections = split_sections(t["segments"])
    steps = [tm["profile"]]
    steps += [tm["section_per_kchar"] * sum(len(s["text"]) for s in sec) / 1000 for sec in sections]
    steps += [tm["overview"], tm["render"]]
    return sections, steps


def build_notes(t: dict, model: str, set_stage, timings: dict | None = None) -> dict:
    """set_stage(text, seconds_left, fraction_done) is called at the start of every step."""
    segs = t["segments"]
    duration = segs[-1]["end"] if segs else 0
    channel = f" by {t['channel']}" if t.get("channel") else ""
    sections, est = plan(t, timings)
    total = sum(est)
    measured = {}

    def step(i: int, text: str):
        set_stage(text, sum(est[i:]), sum(est[:i]) / total if total else 0)

    t0 = time.time()
    step(0, "Figuring out what the video is about")
    opening = "\n".join(s["text"] for s in segs)[:6000]
    prof = llm_json(PROFILE_PROMPT.format(title=t["title"], channel=channel, text=opening), PROFILE_SCHEMA, model, budget=300)
    disc = prof.get("discipline") if prof.get("discipline") in DISCIPLINES else "other"
    components = list(dict.fromkeys(COMPONENTS_FOR[disc] + [c for c in prof.get("extra_components", []) if c in COMPONENT_SCHEMA]))[:5]
    profile = {"subject": prof.get("subject") or DISCIPLINES[disc], "discipline": disc,
               "discipline_name": DISCIPLINES[disc], "format": prof.get("format", "other"),
               "level": prof.get("level", "general audience"), "components": components}
    ctx = dict(title=t["title"], channel=channel, subject=profile["subject"], discipline=profile["discipline_name"],
               level=profile["level"], format=profile["format"])

    measured["profile"] = time.time() - t0
    schema = section_schema(components)
    extra = "".join(f"- {COMPONENT_HELP[c]}\n" for c in components)
    parts, numbers = [], []
    for n, sec in enumerate(sections, 1):
        lo, hi = sec[0]["start"], sec[-1]["end"]
        step(n, f"Writing notes for part {n} of {len(sections)} ({stamp(lo)}–{stamp(hi)} in the video)")
        text = "\n".join(f"[{stamp(s['start'])}] {s['text']}" for s in sec)
        try:
            raw = llm_json(SECTION_PROMPT.format(n=n, total=len(sections), start=stamp(lo), end=stamp(hi),
                                                 extra=extra, text=text, **ctx), schema, model)
        except Exception:
            # One bad section shouldn't sink the whole set of notes; mark it and keep going.
            raw = {"title": f"Part {n}", "summary": "The notes for this part couldn't be written automatically. "
                   "Use the timestamps to watch it, or press Regenerate.", "points": []}
        src = norm(" ".join(s["text"] for s in sec))
        part = {
            "title": (raw.get("title") or f"Part {n}").strip(),
            "summary": (raw.get("summary") or "").strip(),
            "start": lo, "end": hi,
            "cues": _clean_list(raw.get("cues")),
            "points": [{"text": p["text"], "sub": [x for x in _clean_list(p.get("sub")) if adds(x, p["text"])],
                        "t": parse_stamp(p.get("t", ""), lo, hi)}
                       for p in raw.get("points", []) if (p.get("text") or "").strip()],
            "terms": _clean_list(raw.get("terms"), "term", "definition"),
            "quotes": [{"text": q["text"], "t": parse_stamp(q.get("t", ""), lo, hi)} for q in raw.get("quotes", [])
                       if said(q.get("text", ""), src)],
        }
        need = {"formulas": ("latex",), "examples": ("problem",), "processes": ("name",), "code": ("code",),
                "events": ("date", "event"), "people": ("name",), "causes": ("cause", "effect"),
                "arguments": ("claim",), "frameworks": ("name",), "comparisons": ("title",),
                "cases": ("name",), "vocab": ("term", "meaning")}
        for c in components:
            part[c] = _clean_list(raw.get(c), *need.get(c, ()))
        for x in raw.get("numbers", []):
            ev = norm(x.get("evidence", ""))
            val = x.get("value")
            digits = f"{val:g}".replace("-", "") if isinstance(val, (int, float)) else ""
            # A number must appear in its evidence, and the evidence must appear in the transcript.
            if ev and digits and digits.split(".")[0] in ev.replace(",", "") and ev in src:
                numbers.append({"id": len(numbers) + 1, "label": x["label"].strip(), "value": float(val),
                                "unit": x.get("unit", "").strip(), "t": parse_stamp(x.get("t", ""), lo, hi)})
        parts.append(part)

    kchars = sum(len(s["text"]) for sec in sections for s in sec) / 1000
    measured["section_per_kchar"] = (time.time() - t0 - measured["profile"]) / max(kchars, 0.1)
    t_ov = time.time()
    step(len(sections) + 1, "Writing the summary and practice quiz")
    parts_txt = "\n\n".join(
        f"## {p['title']} ({stamp(p['start'])}–{stamp(p['end'])})\n{p['summary']}\n" +
        "\n".join(f"- {x['text']}" for x in p["points"]) +
        "".join(f"\n- Term: {x['term']}: {x['definition']}" for x in p["terms"][:6]) for p in parts)
    nums_txt = "\n".join(f"{x['id']}. {x['label']}: {x['value']:g} {x['unit']} [{stamp(x['t'])}]" for x in numbers) or "(none)"
    ov = llm_json(OVERVIEW_PROMPT.format(duration=stamp(duration), parts=parts_txt, numbers=nums_txt, **ctx),
                  OVERVIEW_SCHEMA, model)

    measured["overview"] = time.time() - t_ov
    step(len(sections) + 2, "Designing your PDF")
    return {
        "timing": measured,
        "video": {k: t.get(k) for k in ("id", "title", "channel", "thumbnail", "source", "language")},
        "duration": duration, "model": model or "Claude", "profile": profile,
        "generated": dt.datetime.now().strftime("%B %-d, %Y"),
        "headline": ov.get("headline", ""), "tldr": ov.get("tldr", ""), "summary": ov.get("summary", ""),
        "takeaways": _clean_list(ov.get("takeaways")), "exam_focus": _clean_list(ov.get("exam_focus")),
        "action_items": _clean_list(ov.get("action_items")), "review": _clean_list(ov.get("review"), "q", "a"),
        "charts": ov.get("charts", []), "sections": parts, "numbers": numbers,
    }


# ---------------------------------------------------------------- charts and math

def _svg(fig) -> str:
    buf = io.StringIO()
    fig.savefig(buf, format="svg", bbox_inches="tight", transparent=True)
    import matplotlib.pyplot as plt
    plt.close(fig)
    svg = re.sub(r"^.*?(<svg)", r"\1", buf.getvalue(), flags=re.S)
    return svg.replace("font-family: 'Inter', 'DejaVu Sans'", "font-family: Inter, 'DejaVu Sans', sans-serif")


def _plt():
    import logging
    import matplotlib
    logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)  # Inter falls back quietly
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({
        "svg.fonttype": "none", "font.family": ["Inter", "DejaVu Sans"], "font.size": 9,
        "axes.spines.top": False, "axes.spines.right": False, "axes.edgecolor": "#c9c9cf",
        "axes.labelcolor": "#55555c", "xtick.color": "#55555c", "ytick.color": "#55555c",
        "mathtext.fontset": "dejavuserif",
    })
    return plt


def math_svg(latex: str) -> str | None:
    """Typeset a formula with matplotlib's mathtext; None if it can't parse it."""
    tex = latex.strip().strip("$")
    tex = re.sub(r"\\(?:text|textrm|operatorname)\{", r"\\mathrm{", tex)
    tex = re.sub(r"\\[dt]frac", r"\\frac", tex).replace("\\,", " ").replace("\\!", "")
    plt = _plt()
    # Draw glyphs as paths: exact math italics and symbols, no dependence on installed fonts.
    with plt.rc_context({"svg.fonttype": "path"}):
        fig = plt.figure(figsize=(0.01, 0.01))
        try:
            fig.text(0, 0, f"${tex}$", fontsize=13, color="#1b1b1f")
            return _svg(fig)
        except Exception:
            plt.close(fig)
            return None


def topic_map_svg(sections: list[dict], duration: float, colors: list[str] | None = None) -> str:
    """Where the video spends its time: one bar per section along the timeline."""
    plt = _plt()
    fig, ax = plt.subplots(figsize=(7.2, 0.3 * len(sections) + 0.5))
    ys = list(range(len(sections)))[::-1]
    for i, (s, y) in enumerate(zip(sections, ys)):
        ax.barh(y, s["end"] - s["start"], left=s["start"], height=0.58,
                color=(colors or [ACCENT, "#f08a86"])[i % len(colors or [0, 0])], edgecolor="white", linewidth=0.8)
    names = [f"{i + 1}. {s['title'][:38]}{'…' if len(s['title']) > 38 else ''}" for i, s in enumerate(sections)]
    ax.set_yticks(ys, names, fontsize=8, color="#1b1b1f")
    ax.tick_params(axis="y", length=0, pad=6)
    ax.grid(axis="x", color="#ececf0", linewidth=0.6)
    ax.set_axisbelow(True)
    ticks = [x for x in range(0, int(duration) + 1, max(60, int(duration / 6) // 60 * 60 or 60))]
    ax.set_xticks(ticks, [stamp(x) for x in ticks])
    ax.set_xlim(0, duration * 1.02)
    ax.spines["left"].set_visible(False)
    return _svg(fig)


def _year(date: str) -> float | None:
    d = date.lower()
    m = re.search(r"(\d{3,4})\s*(bce|bc)\b", d)
    if m:
        return -float(m.group(1))
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th) century\b", d)
    if m:
        return (int(m.group(1)) - 1) * 100 + 50
    m = re.search(r"\b(\d{3,4})s?\b", d)
    return float(m.group(1)) if m else None


def timeline_svg(events: list[dict]) -> str | None:
    """Dated events on one axis, labels alternating above and below. Needs 3+ datable events."""
    pts = sorted({(_year(e["date"]), e["date"], e["event"]) for e in events if _year(e["date"]) is not None})
    if len(pts) < 3:
        return None
    pts = pts[:14]
    plt = _plt()
    fig, ax = plt.subplots(figsize=(7.4, 2.9))
    xs = [p[0] for p in pts]
    span = (max(xs) - min(xs)) or 1
    ax.axhline(0, color="#c9c9cf", linewidth=1.5, zorder=1)
    for i, (x, label, ev) in enumerate(pts):
        up = 1 if i % 2 == 0 else -1
        h = up * (0.55 + 0.35 * ((i // 2) % 2))
        ax.plot([x, x], [0, h], color="#e3e3e8", linewidth=1, zorder=1)
        ax.scatter([x], [0], s=28, color=ACCENT, zorder=3)
        text = ev if len(ev) <= 46 else ev[:44] + "…"
        ax.text(x, h + 0.06 * up, f"{label}\n{text}", ha="center", va="bottom" if up > 0 else "top", fontsize=6.8,
                color="#1b1b1f", wrap=True)
    ax.set_xlim(min(xs) - span * 0.08, max(xs) + span * 0.08)
    ax.set_ylim(-1.6, 1.6)
    ax.axis("off")
    return _svg(fig)


def chart_svg(chart: dict, numbers: list[dict]) -> str | None:
    by_id = {n["id"]: n for n in numbers}
    pts = [by_id[i] for i in chart.get("number_ids", []) if i in by_id]
    units = {p["unit"] for p in pts}
    if len(pts) < 2 or len(units) > 1:
        return None  # nothing honest to plot
    unit = units.pop()
    kind = chart.get("type", "bar")
    if kind == "pie" and (unit != "%" or sum(p["value"] for p in pts) > 101):
        kind = "bar"
    plt = _plt()
    labels = [p["label"][:28] for p in pts]
    vals = [p["value"] for p in pts]
    if kind == "pie":
        fig, ax = plt.subplots(figsize=(4.6, 3.2))
        rest = 100 - sum(vals)
        if rest > 0.5:
            vals, labels = vals + [rest], labels + ["Other"]
        colors = [ACCENT, "#f08a86", "#8f1d1a", "#f6c1bf", "#5c5c66", "#c9c9cf"]
        ax.pie(vals, labels=labels, colors=colors[:len(vals)], autopct="%1.0f%%", startangle=90,
               wedgeprops={"linewidth": 1.5, "edgecolor": "white"}, textprops={"fontsize": 8})
    elif kind == "line":
        fig, ax = plt.subplots(figsize=(6.4, 2.8))
        ax.plot(labels, vals, color=ACCENT, marker="o", linewidth=2)
        for x, v in zip(labels, vals):
            ax.annotate(f"{v:g}", (x, v), textcoords="offset points", xytext=(0, 6), ha="center", fontsize=8)
        ax.set_ylabel(unit)
    else:
        fig, ax = plt.subplots(figsize=(6.4, 0.3 * len(pts) + 0.6))
        y = range(len(pts))[::-1]
        ax.barh(list(y), vals, color=ACCENT, height=0.6)
        ax.set_yticks(list(y), labels)
        for yy, v in zip(y, vals):
            ax.text(v, yy, f" {v:g}{'' if unit in ('', '%') else ' '}{unit}" if unit != "$" else f" ${v:g}",
                    va="center", fontsize=8)
        ax.set_xlabel(unit)
    return _svg(fig)


# ---------------------------------------------------------------- HTML / PDF / markdown

# Three looks for the same notes. Multi-color gives each part its own hue; red is the brand;
# black & white prints cleanly. Every colour on the page comes from these variables.
THEMES = {
    "color": {"accent": "#e5322d", "parts": ["#4f46e5", "#0d9488", "#d97706", "#e11d48", "#7c3aed", "#16a34a"], "ink": "#1d1b1a"},
    "red": {"accent": "#e5322d", "parts": ["#e5322d", "#b91c1c"], "ink": "#1d1b1a"},
    "bw": {"accent": "#1d1b1a", "parts": ["#1d1b1a"], "ink": "#111111"},
}


def tint(hex_color: str, amount: float) -> str:
    """Mix a colour with white: amount=0.1 gives a pale wash for backgrounds."""
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    mix = lambda c: round(255 - (255 - c) * amount)
    return f"#{mix(r):02x}{mix(g):02x}{mix(b):02x}"


CSS = """
@page { size: Letter; margin: 18mm 18mm 20mm;
  @bottom-left { content: "%(title)s"; font: 7.5pt Inter, sans-serif; color: #a19a97; }
  @bottom-right { content: counter(page) " / " counter(pages); font: 7.5pt Inter, sans-serif; color: #a19a97; } }
@page:first { @bottom-left { content: none; } }
* { box-sizing: border-box; }
body { font: 10.6pt/1.62 Inter, "DejaVu Sans", sans-serif; color: %(ink)s; margin: 0; background: #fff; }
a { color: inherit; text-decoration: none; }
p { margin: 0; }
.cover { display: flex; gap: 16px; align-items: center; }
.cover img { width: 132px; border-radius: 8px; }
.cover img.brandmark { width: 44px; height: 44px; border-radius: 11px; align-self: flex-start; }
.eyebrow { font-size: 7.4pt; letter-spacing: .14em; text-transform: uppercase; color: %(accent)s; font-weight: 700; }
h1 { font-size: 21pt; line-height: 1.15; margin: 3px 0 5px; letter-spacing: -.02em; }
.meta { color: #7d7673; font-size: 8.6pt; }
.chips { margin-top: 7px; }
.chip { display: inline-block; font-size: 7.6pt; padding: 2px 9px; border-radius: 99px; background: #f3f1f0; color: #57504d; margin: 0 4px 3px 0; }
.chip.on { background: %(accent_soft)s; color: %(accent)s; font-weight: 600; }
.short { margin: 20px 0 6px; padding: 16px 18px; border-radius: 12px; background: %(accent_soft)s; font-size: 12pt; line-height: 1.55; }
.short b.k { display: block; font-size: 7.4pt; letter-spacing: .14em; text-transform: uppercase; color: %(accent)s; margin-bottom: 4px; }
.headline { font-size: 13.5pt; font-weight: 650; line-height: 1.4; margin: 22px 0 0; letter-spacing: -.01em; }
h2 { font-size: 13pt; margin: 26px 0 10px; letter-spacing: -.01em; break-after: avoid; }
h3 { font-size: 10.5pt; margin: 12px 0 5px; break-after: avoid; }
ol.main { list-style: none; padding: 0; margin: 0; counter-reset: k; }
ol.main li { position: relative; padding: 5px 0 5px 34px; break-inside: avoid; }
ol.main li::before { counter-increment: k; content: counter(k); position: absolute; left: 0; top: 4px; width: 22px; height: 22px; border-radius: 50%%;
  background: var(--a); color: #fff; font-size: 8.4pt; font-weight: 700; text-align: center; line-height: 22px; }
ul.focus { list-style: none; padding: 0; margin: 0; }
ul.focus li { padding: 4px 0 4px 18px; position: relative; }
ul.focus li::before { content: ""; position: absolute; left: 2px; top: 10px; width: 7px; height: 7px; border-radius: 2px; background: %(accent)s; rotate: 45deg; }
figure { margin: 6px 0 2px; break-inside: avoid; }
figure svg { width: 100%%; height: auto; }
figcaption { font-size: 8pt; color: #7d7673; margin-top: 3px; }
/* each part: coloured header, Cornell columns, a tinted summary */
.part { margin-top: 30px; }
/* plain block (not flex) so the PDF engine keeps the header on the same page as its notes */
.part-head { display: block; padding-bottom: 8px; border-bottom: 2px solid var(--a); break-after: avoid; page-break-after: avoid; }
.part-head .n { display: inline-block; width: 26px; height: 26px; border-radius: 8px; background: var(--a); color: #fff; font-weight: 700; font-size: 10pt; text-align: center; line-height: 26px; margin-right: 10px; vertical-align: middle; }
.part-head h2 { display: inline; margin: 0; font-size: 14pt; vertical-align: middle; }
.part-head .t { float: right; margin-top: 6px; font-size: 8.4pt; color: var(--a); font-weight: 600; white-space: nowrap; }
.part-head + .cornell { break-before: avoid; page-break-before: avoid; }
.cornell { margin-top: 12px; }
.cornell::after { content: ""; display: block; clear: both; }
.cues { float: left; width: 28%%; padding: 2px 0 2px 11px; border-left: 3px solid var(--a); font-size: 9.2pt; color: #4d4643; }
.cues .eyebrow { color: var(--a); margin-bottom: 5px; }
.cues ol { margin: 0; padding-left: 14px; } .cues li { margin: 0 0 7px; }
.notes { margin-left: 32%%; }
ul.points { margin: 0; padding: 0; list-style: none; }
ul.points > li { padding: 3px 0 7px 52px; position: relative; }
ul.points ul { margin: 3px 0 0; padding-left: 15px; color: #5d5653; font-size: 9.6pt; }
ul.points ul li { margin: 1px 0; }
.ts { position: absolute; left: 0; top: 4px; font: 600 7.6pt "DejaVu Sans Mono", monospace; color: var(--a); background: var(--a-soft); border-radius: 5px; padding: 1px 5px; }
.ts.inline { position: static; }
.sumbox { clear: both; margin-top: 10px; padding: 10px 14px; border-radius: 10px; background: var(--a-soft); font-size: 10pt; }
.sumbox b { font-size: 7.4pt; letter-spacing: .12em; text-transform: uppercase; color: var(--a); margin-right: 8px; }
blockquote { margin: 8px 0; padding: 2px 0 2px 14px; border-left: 2px solid var(--a); color: #4d4643; font-style: italic; }
.card { border-radius: 10px; padding: 10px 13px; margin: 8px 0; background: #faf9f8; break-inside: avoid; }
.card .lbl { font-size: 7.2pt; letter-spacing: .12em; text-transform: uppercase; color: var(--a); font-weight: 700; margin-bottom: 2px; }
.formula { display: flex; gap: 12px; align-items: center; }
.formula .math svg { height: auto; max-width: 270px; }
.formula .math code { font-size: 10pt; }
ol.steps { margin: 4px 0; padding-left: 18px; } ol.steps li { margin: 2px 0; }
.answer { font-weight: 650; color: #15803d; margin-top: 3px; }
pre { background: #1b1b1f; color: #f2f2f5; border-radius: 8px; padding: 9px 11px; font: 8.4pt/1.45 "DejaVu Sans Mono", monospace; white-space: pre-wrap; margin: 5px 0; }
.brief { display: grid; grid-template-columns: 22%% 78%%; gap: 4px 10px; font-size: 9.6pt; }
.brief dt { font-weight: 700; color: #5d5653; } .brief dd { margin: 0; }
dl.terms { display: grid; grid-template-columns: 27%% 73%%; gap: 5px 12px; margin: 10px 0 0; font-size: 9.6pt; }
dl.terms dt { font-weight: 650; } dl.terms dd { margin: 0; color: #4d4643; }
table { width: 100%%; border-collapse: collapse; font-size: 9.4pt; margin: 6px 0 10px; }
td, th { text-align: left; padding: 6px 8px; border-bottom: 1px solid #efeceb; vertical-align: top; }
th { font-size: 7.4pt; text-transform: uppercase; letter-spacing: .1em; color: #a19a97; font-weight: 600; }
td.num { font-weight: 700; white-space: nowrap; }
ul.todo { list-style: none; padding: 0; } ul.todo li { padding: 4px 0 4px 24px; position: relative; }
ul.todo li::before { content: ""; position: absolute; left: 2px; top: 8px; width: 11px; height: 11px; border: 1.6px solid %(accent)s; border-radius: 3px; }
ul.warn { list-style: none; padding: 0; margin: 6px 0; } ul.warn li { padding: 3px 0 3px 20px; position: relative; }
ul.warn li::before { content: "!"; position: absolute; left: 2px; top: 4px; width: 13px; height: 13px; border-radius: 50%%; background: #f59e0b; color: #fff; font-size: 7.5pt; font-weight: 800; text-align: center; line-height: 13px; }
.whole { margin-top: 30px; padding: 16px 18px; border-radius: 12px; background: #f6f5f4; }
.whole b.k { display: block; font-size: 7.4pt; letter-spacing: .14em; text-transform: uppercase; color: #7d7673; margin-bottom: 4px; }
ol.quiz { padding-left: 0; list-style: none; counter-reset: q; }
ol.quiz li { position: relative; padding: 0 0 4px 34px; margin: 0 0 14px; break-inside: avoid; }
ol.quiz li::before { counter-increment: q; content: counter(q); position: absolute; left: 0; top: 0; width: 22px; height: 22px; border-radius: 7px;
  background: %(accent_soft)s; color: %(accent)s; font-weight: 700; font-size: 8.6pt; text-align: center; line-height: 22px; }
ol.quiz .kind { font-size: 7pt; text-transform: uppercase; letter-spacing: .1em; color: #a19a97; margin-left: 6px; }
ol.quiz .line { border-bottom: 1px dashed #d6d1cf; height: 20px; }
ol.answers { padding-left: 18px; } ol.answers li { margin: 0 0 6px; }
.newpage { break-before: page; }
.foot { margin-top: 28px; padding-top: 10px; border-top: 1px solid #efeceb; font-size: 7.6pt; color: #a19a97; }
"""


def _components_html(s: dict, link) -> str:
    out = []
    for f in s.get("formulas", []):
        svg = math_svg(f["latex"])
        math = svg or f"<code>{html.escape(f['latex'])}</code>"
        out.append(f"<div class='card formula'><div class='math'>{math}</div><div><div class='lbl'>{html.escape(f.get('name') or 'Formula')}</div>"
                   f"{md_inline(f.get('meaning', ''))}<div class='meta'>{html.escape(f.get('variables', ''))}</div></div></div>")
    for e in s.get("examples", []):
        steps = "".join(f"<li>{md_inline(x)}</li>" for x in e.get("steps", []))
        out.append(f"<div class='card'><div class='lbl'>Example, step by step</div><b>{md_inline(e['problem'])}</b>"
                   f"<ol class='steps'>{steps}</ol>" + (f"<div class='answer'>→ {md_inline(e['answer'])}</div>" if e.get("answer") else "") + "</div>")
    for p in s.get("processes", []):
        steps = "".join(f"<li>{md_inline(x)}</li>" for x in p.get("steps", []))
        out.append(f"<div class='card'><div class='lbl'>Steps</div><b>{md_inline(p['name'])}</b><ol class='steps'>{steps}</ol></div>")
    for c in s.get("code", []):
        out.append(f"<div class='card'><div class='lbl'>{html.escape(c.get('language') or 'Code')}</div><pre>{html.escape(c['code'])}</pre>{md_inline(c.get('explanation', ''))}</div>")
    if s.get("events"):
        rows = "".join(f"<tr><td class='num'>{html.escape(e['date'])}</td><td>{md_inline(e['event'])}</td></tr>" for e in s["events"])
        out.append(f"<table><tr><th>When</th><th>What happened</th></tr>{rows}</table>")
    if s.get("causes"):
        rows = "".join(f"<tr><td>{md_inline(c['cause'])}</td><td>→ {md_inline(c['effect'])}</td></tr>" for c in s["causes"])
        out.append(f"<table><tr><th>Cause</th><th>Effect</th></tr>{rows}</table>")
    if s.get("arguments"):
        rows = "".join(f"<tr><td><b>{md_inline(a['claim'])}</b></td><td>{md_inline(a.get('evidence', ''))}</td><td>{md_inline(a.get('counter', '')) or '—'}</td></tr>"
                       for a in s["arguments"])
        out.append(f"<table><tr><th>The point</th><th>Why / proof given</th><th>Other side</th></tr>{rows}</table>")
    for f in s.get("frameworks", []):
        parts = "".join(f"<li>{md_inline(x)}</li>" for x in f.get("parts", []))
        out.append(f"<div class='card'><div class='lbl'>Model to use</div><b>{md_inline(f['name'])}</b><ul>{parts}</ul>{md_inline(f.get('use', ''))}</div>")
    for c in s.get("comparisons", []):
        cols = c.get("columns") or []
        rows = [r for r in c.get("rows", []) if r]
        if cols and rows:
            head = "".join(f"<th>{html.escape(x)}</th>" for x in cols)
            body = "".join("<tr>" + "".join(f"<td>{md_inline(x)}</td>" for x in r[:len(cols)]) + "</tr>" for r in rows)
            out.append(f"<h3>{md_inline(c['title'])}</h3><table><tr>{head}</tr>{body}</table>")
    for c in s.get("cases", []):
        brief = "".join(f"<dt>{k}</dt><dd>{md_inline(c.get(f, ''))}</dd>" for k, f in
                        (("Facts", "facts"), ("Issue", "issue"), ("Rule", "rule"), ("Holding", "holding"), ("Reasoning", "reasoning")) if c.get(f))
        out.append(f"<div class='card'><div class='lbl'>Court case</div><b>{md_inline(c['name'])}</b><dl class='brief'>{brief}</dl></div>")
    if s.get("vocab"):
        rows = "".join(f"<tr><td><b>{html.escape(v['term'])}</b></td><td>{md_inline(v['meaning'])}</td><td><i>{md_inline(v.get('example', ''))}</i></td></tr>" for v in s["vocab"])
        out.append(f"<table><tr><th>Word / phrase</th><th>Meaning</th><th>Example</th></tr>{rows}</table>")
    if s.get("people"):
        out.append("<p class='meta'><b>People:</b> " + "; ".join(f"{html.escape(p['name'])} ({html.escape(p.get('role', ''))})" for p in s["people"]) + "</p>")
    if s.get("mistakes"):
        out.append("<div class='lbl' style='font-size:7.2pt;letter-spacing:.1em;color:#8a8a92;font-weight:700;margin-top:6px'>WATCH OUT FOR</div><ul class='warn'>"
                   + "".join(f"<li>{md_inline(m)}</li>" for m in s["mistakes"]) + "</ul>")
    return "".join(out)


# Words a first-year student or a parent would use for the profile chips.
PLAIN = {"intro": "Beginner", "intermediate": "Intermediate", "advanced": "Advanced", "general audience": "For everyone",
         "lecture": "Lesson", "tutorial": "How-to", "discussion": "Discussion", "interview": "Interview",
         "news": "News", "review": "Review", "documentary": "Documentary"}


def _logo_uri() -> str:
    """Gabe's GM logo for the PDF cover, inlined so the PDF needs no network."""
    f = Path(__file__).parent / "static" / "brand" / "gm-icon-192.png"
    return f"data:image/png;base64,{base64.b64encode(f.read_bytes()).decode()}" if f.exists() else ""


def render_html(nb: dict, theme: str = "color") -> str:
    th = THEMES.get(theme, THEMES["color"])
    v = nb["video"]
    prof = nb.get("profile", {})
    url = f"https://www.youtube.com/watch?v={v['id']}"
    link = lambda t: f"{url}&t={int(t)}s"
    part_color = lambda i: th["parts"][i % len(th["parts"])]
    part_vars = lambda i: f"--a:{part_color(i)};--a-soft:{tint(part_color(i), 0.09 if theme != 'bw' else 0.06)}"
    thumb = ""
    try:
        with urllib.request.urlopen(v["thumbnail"], timeout=8) as r:
            thumb = f'<img src="data:image/jpeg;base64,{base64.b64encode(r.read()).decode()}">'
    except Exception:
        pass

    charts = []
    for c in nb["charts"][:3]:
        svg = chart_svg(c, nb["numbers"])
        if svg:
            charts.append(f"<figure><div class='eyebrow'>{html.escape(c['title'])}</div>{svg}"
                          f"<figcaption>{html.escape(c.get('caption', ''))}</figcaption></figure>")

    parts = []
    for i, s in enumerate(nb["sections"]):
        pts = "".join(
            f"<li><a class='ts' href='{link(p['t'])}'>{stamp(p['t'])}</a>{md_inline(p['text'])}"
            + ("<ul>" + "".join(f"<li>{md_inline(x)}</li>" for x in p.get("sub", [])) + "</ul>" if p.get("sub") else "")
            + "</li>" for p in s["points"])
        cues = "".join(f"<li>{md_inline(q)}</li>" for q in s.get("cues", []))
        quotes = "".join(f"<blockquote>“{html.escape(q['text'])}” <a class='meta' href='{link(q['t'])}'>{stamp(q['t'])}</a></blockquote>" for q in s["quotes"])
        terms = "".join(f"<dt>{html.escape(x['term'])}</dt><dd>{html.escape(x['definition'])}</dd>" for x in s["terms"])
        parts.append(
            f"<section class='part' style='{part_vars(i)}'><div class='part-head'><span class='n'>{i + 1}</span><h2>{html.escape(s['title'])}</h2>"
            f"<a class='t' href='{link(s['start'])}'>{stamp(s['start'])}–{stamp(s['end'])}</a></div>"
            f"<div class='cornell'>" + (f"<aside class='cues'><div class='eyebrow'>Quiz yourself</div><ol>{cues}</ol></aside>" if cues else "")
            + f"<div class='notes' style='{'' if cues else 'margin-left:0'}'><ul class='points'>{pts}</ul>{_components_html(s, link)}{quotes}"
            + (f"<dl class='terms'>{terms}</dl>" if terms else "") + "</div></div>"
            f"<div class='sumbox'><b>In short</b>{md_inline(s['summary'])}</div></section>")

    # Study pages at the back.
    formulas = [f for s in nb["sections"] for f in s.get("formulas", [])]
    events = [e for s in nb["sections"] for e in s.get("events", [])]
    glossary = {}
    for s in nb["sections"]:
        for x in s["terms"]:
            glossary.setdefault(x["term"].strip().lower(), x)
    tl = timeline_svg(events) if events else None
    formula_sheet = "".join(
        f"<div class='card formula'><div class='math'>{math_svg(f['latex']) or '<code>' + html.escape(f['latex']) + '</code>'}</div>"
        f"<div><div class='lbl'>{html.escape(f.get('name') or 'Formula')}</div>{md_inline(f.get('meaning', ''))}</div></div>" for f in formulas)
    gloss = "".join(f"<dt>{html.escape(x['term'])}</dt><dd>{html.escape(x['definition'])}</dd>" for _, x in sorted(glossary.items()))
    numbers = "".join(
        f"<tr><td class='num'>{'$' if n['unit'] == '$' else ''}{n['value']:,g}{'' if n['unit'] in ('$', '') else (n['unit'] if n['unit'] == '%' else ' ' + html.escape(n['unit']))}</td>"
        f"<td>{html.escape(n['label'])}</td><td><a class='ts inline' href='{link(n['t'])}'>{stamp(n['t'])}</a></td></tr>"
        for n in nb["numbers"])
    quiz = "".join(f"<li>{md_inline(r['q'])}<span class='kind'>{html.escape(r.get('kind', ''))}</span><div class='line'></div></li>" for r in nb["review"])
    key = "".join(f"<li>{md_inline(r['a'])}</li>" for r in nb["review"])
    todo = "".join(f"<li>{md_inline(a)}</li>" for a in nb["action_items"])
    focus = "".join(f"<li>{md_inline(a)}</li>" for a in nb["exam_focus"])
    takeaways = "".join(f"<li style='{part_vars(i)}'>{md_inline(k)}</li>" for i, k in enumerate(nb["takeaways"]))
    chips = "".join(f"<span class='chip{' on' if j == 0 else ''}'>{html.escape(str(x))}</span>" for j, x in enumerate(
        [prof.get("subject"), PLAIN.get(prof.get("level"), prof.get("level")), PLAIN.get(prof.get("format"), prof.get("format"))]) if x and x != "other")
    css = CSS % {"accent": th["accent"], "accent_soft": tint(th["accent"], 0.08), "ink": th["ink"],
                 # page footer is CSS text, not HTML: strip what would break the string instead of escaping
                 "title": re.sub(r'["\\]', "'", v["title"])[:70]}
    map_colors = [part_color(i) for i in range(len(nb["sections"]))] if theme == "color" else (
        [th["accent"], tint(th["accent"], 0.55)] if theme == "red" else ["#3a3a3a", "#9a9a9a"])

    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{html.escape(v['title'])} — study notes</title>
<style>{css}</style></head><body>
<div class="cover">{thumb}<div style="flex:1"><div class="eyebrow">Study notes</div><h1><a href="{url}">{html.escape(v['title'])}</a></h1>
<div class="meta">{html.escape(v.get('channel') or '')} · {stamp(nb['duration'])} · {len(nb['sections'])} parts</div>
<div class="chips">{chips}</div></div>{f'<img class="brandmark" src="{_logo_uri()}">' if _logo_uri() else ""}</div>
<div class="short"><b class="k">The short version</b>{md_inline(nb['tldr'])}</div>
<h2>Main points</h2><ol class="main">{takeaways}</ol>
{"<h2>Most important to remember</h2><ul class='focus'>" + focus + "</ul>" if focus else ""}
<h2>What's covered, and when</h2><figure>{topic_map_svg(nb['sections'], nb['duration'], map_colors)}</figure>
{"<h2>Timeline</h2><figure>" + tl + "</figure>" if tl else ""}
{"<h2>By the numbers</h2>" + "".join(charts) if charts else ""}
{"<h2>Do this next</h2><ul class='todo'>" + todo + "</ul>" if todo else ""}
{"".join(parts)}
<div class="whole"><b class="k">The whole video in one paragraph</b>{md_inline(nb.get('summary', ''))}</div>
{"<div class='newpage'></div><h2>All the formulas</h2>" + formula_sheet if formula_sheet else ""}
{"<h2>Words to know</h2><dl class='terms'>" + gloss + "</dl>" if gloss else ""}
{"<h2>Numbers from the video</h2><table><tr><th>Number</th><th>What it is</th><th>When</th></tr>" + numbers + "</table>" if numbers else ""}
{"<div class='newpage'></div><h2>Practice quiz</h2><p class='meta'>Answer from memory first, then check the next page.</p><ol class='quiz'>" + quiz + "</ol>" if quiz else ""}
{"<div class='newpage'></div><h2>Answers</h2><ol class='answers'>" + key + "</ol>" if key else ""}
<div class="foot">Made by GM Study Notes (gmstudynotes.com) from {'the video\'s YouTube subtitles' if v.get('source') == 'captions' else 'listening to the video'}. AI notes can contain mistakes, so check anything important against the video. · {url}</div>
</body></html>"""


def render_pdf(page: str, out: Path) -> bool:
    try:
        import weasyprint
    except Exception:
        return False  # system PDF libraries missing (e.g. pango on a Mac without Homebrew)
    weasyprint.HTML(string=page).write_pdf(out)
    return True


def render_markdown(nb: dict) -> str:
    prof = nb.get("profile", {})
    lines = [f"*{prof.get('subject', '')} · {prof.get('level', '')} {prof.get('format', '')}*", "",
             f"**{nb['headline']}**", "", "## The short version", nb["tldr"], "", "## Main points"]
    lines += [f"{i}. {k}" for i, k in enumerate(nb["takeaways"], 1)]
    if nb.get("exam_focus"):
        lines += ["", "## Most important to remember"] + [f"- {x}" for x in nb["exam_focus"]]
    if nb["action_items"]:
        lines += ["", "## Do this next"] + [f"- [ ] {a}" for a in nb["action_items"]]
    for i, s in enumerate(nb["sections"], 1):
        lines += ["", f"## {i}. {s['title']} ({stamp(s['start'])}–{stamp(s['end'])})"]
        if s.get("cues"):
            lines += ["**Quiz yourself:** " + " · ".join(s["cues"]), ""]
        for p in s["points"]:
            lines.append(f"- [{stamp(p['t'])}] {p['text']}")
            lines += [f"  - {x}" for x in p.get("sub", [])]
        for f in s.get("formulas", []):
            lines.append(f"- **{f.get('name') or 'Formula'}:** $${f['latex']}$$ — {f.get('meaning', '')}")
        for e in s.get("examples", []):
            lines += [f"- **Example:** {e['problem']}"] + [f"  {j}. {x}" for j, x in enumerate(e.get("steps", []), 1)] + [f"  → {e.get('answer', '')}"]
        for p in s.get("processes", []):
            lines += [f"- **Process: {p['name']}**"] + [f"  {j}. {x}" for j, x in enumerate(p.get("steps", []), 1)]
        for e in s.get("events", []):
            lines.append(f"- **{e['date']}:** {e['event']}")
        for a in s.get("arguments", []):
            lines.append(f"- **Claim:** {a['claim']} — *evidence:* {a.get('evidence', '')}" + (f" — *counter:* {a['counter']}" if a.get("counter") else ""))
        for c in s.get("cases", []):
            lines.append(f"- **Case: {c['name']}** — Facts: {c.get('facts', '')} Issue: {c.get('issue', '')} Rule: {c.get('rule', '')} Holding: {c.get('holding', '')}")
        for v in s.get("vocab", []):
            lines.append(f"- **{v['term']}** — {v['meaning']} (*{v.get('example', '')}*)")
        for m in s.get("mistakes", []):
            lines.append(f"- ⚠️ {m}")
        lines += [f"> “{q['text']}” [{stamp(q['t'])}]" for q in s["quotes"]]
        lines += [f"- **{x['term']}**: {x['definition']}" for x in s["terms"]]
        lines += ["", f"**In short:** {s['summary']}"]
    lines += ["", "## The whole video in one paragraph", nb.get("summary", "")]
    if nb["numbers"]:
        lines += ["", "## Numbers from the video"]
        lines += [f"- {n['value']:,g} {n['unit']} — {n['label']} [{stamp(n['t'])}]" for n in nb["numbers"]]
    if nb.get("review"):
        lines += ["", "## Practice quiz"] + [f"{i}. {r['q']}" for i, r in enumerate(nb["review"], 1)]
        lines += ["", "## Answers"] + [f"{i}. {r['a']}" for i, r in enumerate(nb["review"], 1)]
    return "\n".join(lines)
