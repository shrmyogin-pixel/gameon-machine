"""Loads config.toml and .env. Every path is a pathlib.Path."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = PROJECT_ROOT / "config.toml"
ENV_FILE = PROJECT_ROOT / ".env"


def expand_path(value: str) -> Path:
    """Turn '$HOME\\x', '~/x' or a relative path into an absolute Path."""
    home = str(Path.home())
    text = value.replace("$HOME", home).replace("%USERPROFILE%", home)
    path = Path(os.path.expandvars(text)).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


@dataclass
class Config:
    raw: dict
    data_dir: Path
    database: Path
    log_file: Path
    clips_dir: Path
    sessions_dir: Path
    whisper_cli: Path
    whisper_model_en: Path
    whisper_model_multi: Path
    ffmpeg: str
    streamlink: str
    claude: str

    @property
    def exports_dir(self) -> Path:
        """Where `export` writes upload packages (data/exports by default)."""
        return expand_path(self.raw.get("paths", {}).get("exports_dir", "data/exports"))

    @property
    def streams(self) -> list[dict]:
        return self.raw.get("streams", [])

    @property
    def feeds(self) -> list[dict]:
        return self.raw.get("feeds", [])

    @property
    def slots(self) -> list[dict]:
        return self.raw.get("slots", [])

    @property
    def recorder(self) -> dict:
        defaults = {
            "check_interval_seconds": 60,
            "segment_seconds": 10,
            "keep_hours": 1,
            "quality": "720p60,720p,best",
            "twitch_low_latency": True,
        }
        return {**defaults, **self.raw.get("recorder", {})}

    @property
    def clip(self) -> dict:
        defaults = {"context_before": 75, "context_after": 45, "workers": 2,
                    "min_seconds": 30, "max_seconds": 40}
        return {**defaults, **self.raw.get("clip", {})}

    @property
    def ai(self) -> dict:
        defaults = {"model": "sonnet", "min_score": 6, "launch_mode": True, "launch_min_score": 4,
                    "timeout_seconds": 240, "retries": 1}
        return {**defaults, **self.raw.get("ai", {})}

    @property
    def notify(self) -> dict:
        return {"enabled": True, "min_score": 8.0, "banger": True, **self.raw.get("notify", {})}

    @property
    def volume(self) -> dict:
        """~15 Shorts/day: news_per_day from the news runs, live_max_per_day from stream clips."""
        return {"daily_target": 15, "news_per_day": 6, "live_max_per_day": 9, "live_cap_override_score": 9.0,
                **self.raw.get("volume", {})}

    @property
    def news(self) -> dict:
        return {"hours": 12, "stories": 4, "script_min_words": 80, "script_max_words": 105, "target_seconds": 35,
                "voice": "en-US-AndrewNeural", "rate": "+6%", "groq_model": "whisper-large-v3-turbo",
                "max_candidates": 40, "auto_run": False, "run_times": [],
                "images": True, "image_timeout": 15, **self.raw.get("news", {})}

    @property
    def news_feeds(self) -> list[dict]:
        """[[news.feeds]] if given, else the top-level [[feeds]] list."""
        return self.raw.get("news", {}).get("feeds") or self.raw.get("feeds", [])

    @property
    def render(self) -> dict:
        return {"max_renders": 1, **self.raw.get("render", {})}

    def whisper_model_for(self, lang: str) -> Path:
        """English streams use the faster English-only model; everything else the multilingual one."""
        return self.whisper_model_en if (lang or "en").lower().split("-")[0] == "en" else self.whisper_model_multi


def load_config(path: Path = CONFIG_FILE) -> Config:
    with path.open("rb") as f:
        raw = tomllib.load(f)
    paths = raw.get("paths", {})
    tools = raw.get("tools", {})
    return Config(
        raw=raw,
        data_dir=expand_path(paths.get("data_dir", "data")),
        database=expand_path(paths.get("database", "data/clips.sqlite")),
        log_file=expand_path(paths.get("log_file", "data/logs/clipper.log")),
        clips_dir=expand_path(paths.get("clips_dir", "data/clips")),
        sessions_dir=expand_path(paths.get("sessions_dir", "data/sessions")),
        whisper_cli=expand_path(tools["whisper_cli"]),
        whisper_model_en=expand_path(tools["whisper_model_en"]),
        whisper_model_multi=expand_path(tools["whisper_model_multi"]),
        ffmpeg=tools.get("ffmpeg", "ffmpeg"),
        streamlink=tools.get("streamlink", "streamlink"),
        claude=tools.get("claude", "claude"),
    )


def env_key_status(name: str = "GROQ_API_KEY") -> str:
    """Return 'set' or 'not set'. Never returns or logs the key itself."""
    try:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(f"{name}="):
                return "set" if line.split("=", 1)[1].strip().strip("\"'") else "not set"
    except OSError:
        pass
    return "set" if os.environ.get(name) else "not set"


def get_env_key(name: str) -> str | None:
    """The value of a key from .env or the environment, or None. The caller must never print or log it."""
    try:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(f"{name}="):
                value = line.split("=", 1)[1].strip().strip("\"'")
                return value or None
    except OSError:
        pass
    return os.environ.get(name) or None
