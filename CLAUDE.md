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
