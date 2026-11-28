# gameon-clipper — Project Rules

Tool that clips gaming news, leaks, free offers and live streams into 30–40s vertical Shorts
for the **Gameon** channel. Read `voice.md` for script/voice rules.

## Zero-Ban Policy Shield (non-negotiable)

1. **Transformative edits only.** Every clip must add original value: commentary, context,
   captions, reframing, cropping to vertical, or analysis. Never re-upload raw footage unchanged.
2. **Mandatory credit line.** Every export's description/metadata ends with a line starting
   `Sources & Credits:` naming the source outlet/streamer/platform and link or handle.
   An export without it is invalid and must not be produced.
3. **Hard-skip content.** Skip the clip entirely (log reason, do not edit, do not export) if it contains:
   slurs or hate speech, harassment or targeted abuse, gambling or betting promotion,
   minors (children featured or sexualised/unsafe content involving them), or gore/graphic violence.
4. **Mask profanity in captions.** Profanity appears as `s***`, `f***`, etc. in burned-in captions
   and any text output.
5. **`autopost = false`.** The tool never uploads or publishes anything. A human reviews and
   uploads manually. Do not add code that posts automatically.
6. **Hardware FFmpeg encoding at below-normal priority.** Prefer hardware encoders
   (`h264_nvenc`, `h264_qsv`, `h264_amf`), falling back to `libx264` only if none work.
   Launch every FFmpeg process with Windows `BELOW_NORMAL_PRIORITY_CLASS` so the PC stays usable.
7. **Three Melbourne peak posting slots (AEDT, UTC+11):**
   - **Slot 1 — US Peak:** 10:00 AM AEDT
   - **Slot 2 — India Peak:** 12:00 AM AEDT (midnight)
   - **Slot 3 — Global Peak:** 2:00 AM AEDT

## Engineering rules

- Windows 10/11, PowerShell, Python 3.13 in `.venv`, dependencies in `requirements.txt`.
- Use `pathlib.Path` for every path. Never build paths with string concatenation or hard-coded backslashes.
- External tools: `streamlink`, `ffmpeg`, `whisper-cli` (whisper.cpp), and Claude Code CLI
  (headless, as the AI editor).
- One settings file: `config.toml`. One database: `data/clips.sqlite`. Logs: `data/logs/clipper.log`.
- Each clip lives in `data/clips/<platform>-<handle>-<YYYYMMDD-HHMMSS>/` containing
  `clip.mp4`, `thumb.jpg`, `frames.jpg`, `analysis.json`.
- Run as `python -m clipper <command>`.
- **Never crash the whole tool because one stream or one clip failed.** Catch, log, continue.
- **Secrets:** never print or log API keys or the contents of `.env`. Only report "set" / "not set".

## Shorts editing workflow (the only one)

Follow this for any video work here. It merges the `remotion` plugin and the `remotion-motion-graphics` skill
(`.claude/skills/remotion-motion-qa/`); do not run either one's own workflow or checklist on top of it.
When rules clash, the first wins: Zero-Ban Shield > `gameon-pipeline` skill and `voice.md` > this section >
Remotion plugin > motion skill.

### 1. Choose the engine
- **Default: the Python + FFmpeg pipeline.** All news and live Shorts, and any change to their look or pacing.
- **Remotion only when the user asks for it by name** (e.g. a standalone intro or motion graphic). Node.js is not part
  of this project: ask before installing it or scaffolding, keep that project in its own subfolder, and have the user
  check the Remotion licence applies to them.

### 2. Plan and edit
Look, captions, cut lengths, pictures and credits are locked in `gameon-pipeline`; script and closing are in `voice.md`.
Motion rules on top, for new or changed visuals:
- Something moves within the first 0.5 s, and the hook is the boldest picture.
- No still sits frozen: use the existing Ken Burns moves. Calm beats between fast ones; never add motion everywhere.
- Short, clean transitions (8-14 frames). Entrances ease; exits are quicker than entrances.
- One focal point per frame. Draw icons rather than using emoji.
- Key text stays at least 80 px from the sides, below y=190 and above y=1450.
- Sound is the voiceover plus the credited music bed. No sound effects unless asked; then only with a known licence.

The motion skill's purple/cyan theme, five-layer grade and grain stack, glow, 65%-height captions, 30 s scene plan,
CTA ending and 90-frame rule do **not** apply to Game On Shorts.

### 3. Remotion only: code conventions
- Load `remotion:remotion-best-practices` for APIs; it beats the motion skill's `motion-patterns.md`, which is for
  design ideas only (do not copy its code or `theme.ts` unchanged).
- Scaffold with `npx create-video@latest`; add packages with `npx remotion add`.
- `<Video>` / `<Audio>` from `@remotion/media` (not `<OffthreadVideo>`), `<CanvasImage>` for stills, `staticFile()`,
  `@remotion/google-fonts` or local fonts.
- `useCurrentFrame()` + `interpolate()` inline in `style`, clamped, with `Easing.bezier` / `Easing.spring` on moves;
  `scale` / `translate` / `rotate`, not `transform` strings. No CSS transitions or animations. Timing from `fps`.
- Brand colours and fonts in one theme file (white, gold #FFD400, black).
- Open Remotion Studio for preview, with Remotion's own browser. Stills for checking are fine; render the full MP4 only
  when asked, at below-normal priority. The delivered file is encoded as Zero-Ban rule 6 says, and has a credit line.

### 4. Check the output (required after any visual change)
1. Render. In the pipeline, post-render QA (`qa.py`) covers size, decoding and the credit line; read its warnings.
2. Extract a contact sheet from the finished MP4 into the scratchpad (never into the clip folder), at below-normal priority:
   ```
   .venv\Scripts\python.exe -c "import sys; from clipper.config import load_config; from clipper.render import _ffmpeg_out; print(_ffmpeg_out(load_config(), ['-i', sys.argv[1], '-vf', 'fps=1/3,scale=270:480,tile=4x4', '-frames:v', '1', '-update', '1', sys.argv[2]])[0])" <clip.mp4> <scratchpad>\sheet.png
   ```
3. Also extract full-size frames at 0.5 s, the last second, each changed moment and anything doubtful (same call with
   `'-ss', '<seconds>'` before `-i` and no `-vf`). Remotion before an MP4 exists: `npx remotion render <id> <dir> --frames=0,30,90`.
4. **Open and look at every image.** Check: text clipped, overlapping or outside the safe area; captions, pill, source
   line and watermark readable and in place; other people's banners, watermarks or sponsor logos; black, blurry or
   frozen frames; anything on the hard-skip list.
5. Fix, re-render, re-extract, look again. Frames cannot show motion or sound, so say those still need a human watch.
