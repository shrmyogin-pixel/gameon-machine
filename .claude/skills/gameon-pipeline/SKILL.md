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
- `[render] max_renders = 1`, held across processes too (`render._machine_slot`).
- `[general] autopost = false`. Never add code that uploads or publishes.

## Look
- **Captions: `refs/no change.JPG` is the reference. Do not restyle or move them.**
  Word-by-word, 2-3 words, Impact 100, white with the spoken word in cyan (yellow for numbers and trigger words),
  dark navy outline with magenta shadow, centred at y=1180; small `Source: <outlet>` line at y=1330.
  Code: `render.pop_caption_events`, style `Cap` in `news_render.build_news_ass`.
- **Category pill: normal solid yellow fill, dark text.** Opaque box (ASS `BorderStyle 3`), fill `&H0000D4FF&`,
  text `&H00141414&`, no glow, no neon, no shadow. `refs/change 1.JPG` shows the old glowing pill: do not go back to it.
- Pill words (`buckets.badge_label`): `GTA 6`, `FREE GAME`, `PATCH UPDATE`, `WILD BUG`, `CREATOR NEWS`, `GAMING TECH`.
  A flagship story that is not about GTA shows `BIG GAME`; GTA Online and older GTA games show `GTA`.
- Game On watermark bottom-left, above the y=1450 safe zone.

## Pictures and hooks
- 8-10 real pictures per news Short (at least 5, else skip the story, unless it has 3 or more video clips). No placeholders or generated cards.
- Reject low-res (under 1000x560 either way up), blurry, odd-shaped and duplicate pictures (`newsimage.save_picture`).
- Never a picture from, or naming, a betting or casino business (`newsimage.GAMBLING_SOURCE`). A sponsor logo inside
  an ordinary photo is not detected: check pictures in review.
- Never a thumbnail of someone else's video or stream (`newsimage.is_video_thumbnail`).
- Never an outlet's own promo graphic (subscribe / follow / newsletter banners).
- A Short avoids pictures that Shorts of the last 48 hours showed, reusing one only to avoid having too few.
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

## GTA-only mode
- `gta.bat` or `python -m clipper news --only-gta`: stories with GTA in the headline (`gta.py`), looking back
  `[news] gta_hours`, no trend Shorts. Same filters, look, queue and review as `news.bat`, which is unchanged.
- Several GTA Shorts are allowed when each covers a different angle.

## Creators-only mode
- `creators.bat` or `python -m clipper news --creators`: creator, streamer and esports headlines about any game
  (`creators.py`), looking back `[news] creators_hours`, no trend Shorts, every pill `CREATOR NEWS`.
- Never real people's arrests, allegations, abuse, deaths or private life. One Short per creator or event.
- `news.bat`, `export.bat` and the GTA-only filter are unaffected.

## Video clips (every command)
- `videosearch.py` gives each news Short as many clips as its length needs (at most 15): own recordings in `assets/gameplay/<game-slug>/`, then VERIFIED
  YouTube channels through `yt-dlp` (`[news] youtube`), Steam store trailers, Wikimedia Commons.
- Clips are 3-4 s of the liveliest moments (never the first 5 s or last 10 s), muted, reframed to 1080x1920 over a
  blurred fill. Stills stay 2-2.5 s with a Ken Burns move. No signing in, no age-restricted or live videos.
- Every video source is named on the `Sources & Credits:` line itself.
- Games the Steam store marks for gore or sexual content get no Steam trailers. Check all footage in review.
- `tech.bat` or `python -m clipper news --topic tech`: gaming tech headlines only, pill `GAMING TECH`.

## Promo mode
- `promo.bat` or `python -m clipper news --topic promo`: deals, discounts, bundles and gear spotlights, pill
  `GAME DEAL` or `TECH PICK` (same solid yellow).
- Isolated: promo Shorts are invisible to the other commands' repeat checks and daily quota, and never take a YouTube
  slot automatically. A promo run does avoid stories the other commands already made.
- No monetised links, codes or disclosures anywhere in the code or upload sheets.

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
