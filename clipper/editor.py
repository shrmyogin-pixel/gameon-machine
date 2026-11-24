"""Headless Claude AI editor: reads payload.json + frames.jpg, writes analysis.json.

Claude runs as `claude -p` (non-interactive) with the prompt on stdin and only the Read tool, limited
to the clip folder, so it can look at frames.jpg. Output must be strict JSON; anything else is retried
and then the clip is marked failed. Nothing here ever posts or uploads (autopost = false).
Transcript text is never written to the log.
"""
from __future__ import annotations

import json
import logging
import math
import shutil
import subprocess
import sys
from pathlib import Path

from .config import Config
from .db import record_analysis, set_clip_status
from .profanity import mask
from .proc import BELOW_NORMAL_PRIORITY_CLASS, CREATE_NO_WINDOW

log = logging.getLogger("clipper")

PROMPT_TEMPLATE = """You are the editor of Game On, a YouTube Shorts & Reels gaming news and live clipping channel. Decide whether a moment is worth posting and cut it precisely.

STREAM / SOURCE
- Streamer/Channel: {streamer} | Platform: {platform} | Game: {game}
- Stream title: {stream_title} | Link: {stream_link}
- Trigger: spike at t={spike_t}s (window length: {window_len}s)

WHAT TO PRIORITIZE
1. Breaking gaming news, official game reveals, patch notes, free game/Game Pass/PS Plus offers, or confirmed leaks.
2. High-energy reactions, jaw-dropping discoveries, funny fails, or first-looks at new maps/mechanics.

CHAT + AUDIO TIMELINE (5s buckets):
{timeline_rows}

TRANSCRIPT (with timestamps):
{transcript_rows}

VIDEO FRAMES: Use the attached contact sheet to verify what is on screen. Never invent facts not visible or spoken.

TASK
Pick the single best self-contained 20–45s moment (hard max 90s). Start on a sentence boundary; end after the reaction or key fact lands.
Set post=false for: raids, ads, BRB screens, dead air, tech issues, slurs, harassment, real-money gambling, minors, doxxing, or graphic gore.
Set sensitive=true (with sensitive_reason) for suggestive in-game content requiring human review.

TITLE: 25–45 chars (max 60). Name the game, streamer, or platform in the first 4 words. No robotic AI words ("epic", "hilarious", "you won't believe"). Max 1 emoji. No profanity.
HOOK: 2–7 words UPPERCASE burned on video (different words from title, no emoji, no profanity).
MEME_CAPTION: 2–4 short lines (max 32 chars each, sentence case, no emoji).
DESCRIPTION: 1 punchy line (max 90 chars) + exact source attribution ("Watch {streamer} live: {stream_link}").
TAGS: 8–15 relevant search tags.

Reply with STRICT JSON only (no prose, no code fences):
{"post": true, "score": 7, "banger": false, "sensitive": false, "sensitive_reason": "", "start": 12.5, "end": 41.0, "title": "...", "hook": "...", "meme_caption": ["...", "..."], "description": "...", "tags": ["..."], "hashtags": ["#..."], "reason": "one sentence"}"""

HARD_MAX_SECONDS = 90.0
RETRY_NOTE = ("\n\nYour previous reply was rejected: {why}. Reply again with ONLY the JSON object "
              "- no prose, no markdown, no code fences.")


class EditorError(Exception):
    pass


# ------------------------------------------------------------------ prompt
def build_prompt(payload: dict, frames: Path) -> str:
    stream, moment, clip = payload["stream"], payload["moment"], payload["clip"]
    timeline_rows = "\n".join(
        f"t={r[0]}s chat={r[1]} unique={r[2]} hype_msgs={r[3]} loud_db={'-' if r[4] is None else r[4]}"
        for r in payload.get("timeline", [])
    ) or "(no chat or audio data)"
    # word-level, compact: ~10 words per line, each word as start_seconds:word
    words = payload.get("transcript", {}).get("words", [])
    transcript_rows = "\n".join(
        " ".join(f"{w[0]:g}:{w[2]}" for w in words[i:i + 10]) for i in range(0, len(words), 10)
    ) or "(no speech detected)"
    values = {
        "streamer": stream["handle"], "platform": stream["platform"],
        "game": stream.get("game") or "unknown", "stream_title": stream.get("title") or "unknown",
        "stream_link": stream["url"], "spike_t": moment["spike_at_s"], "window_len": clip["duration_s"],
        "timeline_rows": timeline_rows, "transcript_rows": transcript_rows,
    }
    text = PROMPT_TEMPLATE
    # plain replace (not str.format): the template contains literal JSON braces
    for key in ("timeline_rows", "transcript_rows"):  # these first, so their text is never re-expanded
        text = text.replace("{" + key + "}", "\0" + key + "\0")
    for key, val in values.items():
        if key not in ("timeline_rows", "transcript_rows"):
            text = text.replace("{" + key + "}", str(val))
    for key in ("timeline_rows", "transcript_rows"):
        text = text.replace("\0" + key + "\0", str(values[key]))
    return (text + f"\n\nThe contact sheet is the image file {frames.resolve()} - open it with the Read tool "
            f"before answering. Layout: {payload.get('frames', {}).get('layout', '')}.")


# ------------------------------------------------------------------ claude call
def call_claude(cfg: Config, prompt: str, folder: Path) -> str:
    """One headless call. Returns Claude's reply text; raises EditorError on any failure."""
    ai = cfg.ai
    exe = shutil.which(cfg.claude) or cfg.claude
    cmd = [exe, "-p", "--model", str(ai["model"]), "--output-format", "json", "--no-session-persistence",
           "--tools", "Read", "--allowedTools", "Read", "--add-dir", str(folder.resolve()),
           "--permission-mode", "dontAsk"]
    flags = (CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS) if sys.platform == "win32" else 0
    try:
        p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=int(ai["timeout_seconds"]), creationflags=flags, cwd=str(folder))
    except subprocess.TimeoutExpired:
        raise EditorError(f"claude timed out after {ai['timeout_seconds']}s")
    except (OSError, ValueError) as exc:
        raise EditorError(f"could not run claude: {exc}")
    if p.returncode != 0:
        raise EditorError(f"claude exited {p.returncode}: {(p.stderr or p.stdout).strip()[-200:]}")
    try:
        envelope = json.loads(p.stdout)
    except ValueError:
        raise EditorError("claude output was not the expected JSON envelope")
    if not isinstance(envelope, dict) or envelope.get("is_error"):
        raise EditorError(f"claude reported an error: {str(envelope.get('result', ''))[:200]}")
    result = envelope.get("result")
    if not isinstance(result, str) or not result.strip():
        raise EditorError("claude returned an empty reply")
    return result


# ------------------------------------------------------------------ validation
def _num(d: dict, key: str) -> float:
    v = d.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise EditorError(f"'{key}' must be a number")
    return float(v)


def _bool(d: dict, key: str) -> bool:
    if not isinstance(d.get(key), bool):
        raise EditorError(f"'{key}' must be true or false")
    return d[key]


def _str(d: dict, key: str, allow_empty: bool = False) -> str:
    v = d.get(key)
    if not isinstance(v, str) or (not allow_empty and not v.strip()):
        raise EditorError(f"'{key}' must be a non-empty string")
    return v.strip()


def _str_list(d: dict, key: str, lo: int, hi: int) -> list[str]:
    v = d.get(key)
    if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
        raise EditorError(f"'{key}' must be a list of non-empty strings")
    if not lo <= len(v) <= hi:
        raise EditorError(f"'{key}' needs {lo}-{hi} items, got {len(v)}")
    return [x.strip() for x in v]


def validate(reply: str, duration: float, credit: str, streamer: str, link: str) -> dict:
    """Strict parse + type/range checks. Returns the cleaned result (profanity masked, credit added)."""
    try:
        data = json.loads(reply.strip())
    except ValueError:
        raise EditorError("reply was not a bare JSON object")
    if not isinstance(data, dict):
        raise EditorError("reply JSON must be an object")
    out: dict = {"post": _bool(data, "post"), "banger": _bool(data, "banger"), "sensitive": _bool(data, "sensitive")}
    score = _num(data, "score")
    if not 0 <= score <= 10:
        raise EditorError("'score' must be 0-10")
    out["score"] = score
    out["sensitive_reason"] = mask(_str(data, "sensitive_reason", allow_empty=True))
    if out["sensitive"] and not out["sensitive_reason"]:
        raise EditorError("sensitive=true needs a sensitive_reason")
    out["reason"] = mask(_str(data, "reason"))
    start, end = _num(data, "start"), _num(data, "end")
    if out["post"]:
        if start < 0 or end <= start:
            raise EditorError("'start'/'end' must satisfy 0 <= start < end")
        if end > duration + 0.5:
            raise EditorError(f"'end' is past the clip length ({duration:.1f}s)")
        if end - start > HARD_MAX_SECONDS:
            raise EditorError(f"moment longer than the {HARD_MAX_SECONDS:.0f}s hard max")
    out["start"], out["end"] = round(start, 2), round(min(end, duration), 2)
    title = mask(_str(data, "title"))
    if len(title) > 60:
        raise EditorError("'title' longer than 60 chars")
    hook = mask(_str(data, "hook")).upper()
    if not 2 <= len(hook.split()) <= 7:
        raise EditorError("'hook' must be 2-7 words")
    caption = [mask(x) for x in _str_list(data, "meme_caption", 2, 4)]
    if any(len(x) > 32 for x in caption):
        raise EditorError("'meme_caption' lines must be max 32 chars")
    line = mask(_str(data, "description")).splitlines()[0]
    if len(line) > 90:
        line = line.split(" Watch ")[0].strip()  # model may have packed the attribution into the line
        if len(line) > 90:
            raise EditorError("'description' line longer than 90 chars")
    tags = [mask(t) for t in _str_list(data, "tags", 8, 15)]
    hashtags = ["#" + mask(h).lstrip("#").replace(" ", "") for h in _str_list(data, "hashtags", 0, 30)]
    out.update(title=title, hook=hook, meme_caption=caption, description=line, tags=tags, hashtags=hashtags)
    # mandatory attribution (policy rule 2) is added by us, never trusted to the model
    out["attribution"] = f"Watch {streamer} live: {link}"
    out["credit_line"] = credit
    out["description_full"] = f"{line}\n{out['attribution']}\n{credit}"
    return out


def live_cap_reached(cfg: Config, result: dict) -> bool:
    """True when today's live-clip quota is full and this clip is not strong enough to jump the queue."""
    from .db import count_accepted_since
    from .routing import day_start_utc
    v = cfg.volume
    cap = int(v["live_max_per_day"])
    if cap <= 0 or result["banger"] or result["score"] >= float(v["live_cap_override_score"]):
        return False
    return count_accepted_since(cfg.database, day_start_utc(cfg), news=False) >= cap


def decide(cfg: Config, result: dict, capped: bool = False) -> tuple[str, str | None]:
    """-> (status, skip_reason). status is 'edited' (worth exporting) or 'skipped'."""
    ai = cfg.ai
    threshold = float(ai["launch_min_score"] if ai["launch_mode"] else ai["min_score"])
    if not result["post"]:
        return "skipped", f"editor said no: {result['reason']}"[:300]
    if result["score"] < threshold:
        return "skipped", f"score {result['score']:g} below min {threshold:g}"
    if capped:
        return "skipped", (f"daily live quota ({cfg.volume['live_max_per_day']}) full and score {result['score']:g}"
                           f" is below {float(cfg.volume['live_cap_override_score']):g}")
    return "edited", None


# ------------------------------------------------------------------ entry point
def edit_clip(cfg: Config, folder: Path) -> str:
    """Run the editor on a clip folder; returns the final status. Never raises."""
    name = folder.name
    try:
        if cfg.raw.get("general", {}).get("autopost", False):
            log.warning("config has autopost=true; ignored - this tool never uploads")
        payload = json.loads((folder / "payload.json").read_text(encoding="utf-8"))
        frames = folder / "frames.jpg"
        if not frames.is_file():
            raise EditorError("frames.jpg is missing")
        base = build_prompt(payload, frames)
        stream = payload["stream"]
        duration = float(payload["clip"]["duration_s"])
        prompt, last = base, "unknown"
        for attempt in range(1, int(cfg.ai["retries"]) + 2):
            try:
                reply = call_claude(cfg, prompt, folder)
                result = validate(reply, duration, stream["credit_line"], stream["handle"], stream["url"])
                break
            except EditorError as exc:
                last = str(exc)
                log.warning("[%s] editor attempt %d failed: %s", name, attempt, last)
                prompt = base + RETRY_NOTE.format(why=last)
        else:
            raise EditorError(last)
        status, reason = decide(cfg, result, live_cap_reached(cfg, result))
        result.update(status=status, skip_reason=reason, model=cfg.ai["model"], autopost=False,
                      needs_human_review=bool(result["sensitive"]))
        (folder / "analysis.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        set_clip_status(cfg.database, name, status, None, reason)
        record_analysis(cfg.database, name, result, folder / "analysis.json")
        log.info("[%s] editor: %s score=%g%s%s", name, status, result["score"],
                 " SENSITIVE-review" if result["sensitive"] else "", f" ({reason})" if reason else "")
        return status
    except Exception as exc:  # never crash the worker
        log.error("[%s] editor failed: %s", name, exc)
        try:
            set_clip_status(cfg.database, name, "failed", f"editor: {exc}"[:300])
        except Exception:
            pass
        return "failed"
