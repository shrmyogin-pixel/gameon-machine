---
name: gameon-pipeline
description: Locked rules for the Game On Shorts pipeline (1080x1920, h264_qsv, captions, yellow category pill, credits, autopost off, delete on Reject / Mark Uploaded). Use before changing rendering, captions, badges, images, hooks, story selection, export or review in this repo.
---

# Game On pipeline rules

These are fixed. Change one only when the user explicitly asks for that change; then update this file and
`tests/test_rules.py` in the same commit. Script and voice rules live in `voice.md`; safety rules in `CLAUDE.md`.

## Render
- Output is 1080x1920 (`render.W`, `render.H`), 30 fps, for every render and export.
- Encoder: Intel `h264_qsv` on this PC (first working entry of `[encoding] encoders`; `libx264` only if none work).
- Every FFmpeg process runs at below-normal priority (`[encoding] below_normal_priority = true`).
- `[render] max_renders = 1`.
- `[general] autopost = false`. Never add code that uploads or publishes.

## Look
- **Captions: `refs/no change.JPG` is the reference. Do not restyle or move them.**
  Word-by-word, 2-3 words, Impact 100, white with the spoken word in cyan (yellow for numbers and trigger words),
  dark navy outline with magenta shadow, centred at y=1180; small `Source: <outlet>` line at y=1330.
  Code: `render.pop_caption_events`, style `Cap` in `news_render.build_news_ass`.
- **Category pill: normal solid yellow fill, dark text.** Opaque box (ASS `BorderStyle 3`), fill `&H0000D4FF&`,
  text `&H00141414&`, no glow, no neon, no shadow. `refs/change 1.JPG` shows the old glowing pill: do not go back to it.
- Pill words (`buckets.badge_label`): `GTA 6`, `FREE GAME`, `PATCH UPDATE`, `WILD BUG`, `CREATOR NEWS`, `GAMING TECH`.
  A flagship story that is not about GTA shows `BIG GAME`.
- Game On watermark bottom-left, above the y=1450 safe zone.

## Pictures and hooks
- 8-10 real pictures per news Short (at least 5, else skip the story). No placeholders or generated cards.
- Reject low-res (under 1000x560 either way up), blurry, odd-shaped and duplicate pictures (`newsimage.save_picture`).
- Hooks: curiosity gap or concrete stake, never the headline read back, no vague "Nobody expected this" teaser,
  and the first script sentence adds a new fact (`hookscore.py`, `news.validate_reply`).

## Stories
- Six buckets (`buckets.py`): flagship, freebie, update, glitch, creator, tech. At most 2 per bucket per run.
- Never gambling or betting, piracy, cheat tools or scams (`news.HARD_SKIP`), and the other hard skips in `CLAUDE.md`.
- Only facts the source supports; leaks are labelled as leaks.
- A story that cannot answer its own hook (the platform, name, price or date it teases is not in the source) is
  dropped before rendering (`answers_hook`).
- A game covered in the last 24 hours returns only for a first-hand announcement, never a report, claim or leak
  (`topics.is_new_announcement`).

## Credit and attribution
- Every export's description ends with a `Sources & Credits:` line naming the source and its link. No line, no export.
- Music is CC-BY: its attribution line goes into the description automatically and survives manual edits at export.

## Review
- **Reject** and **Mark Uploaded** delete the clip folder immediately (`lifecycle.discard`), after the story is
  recorded so it is never made again. Copies in `data/exports` are never touched.

## Before committing
```
.venv\Scripts\python.exe -m unittest discover tests
.venv\Scripts\python.exe -m clipper doctor
```
Keep `.venv` to `requirements.txt`; add no tool or framework without being asked.
