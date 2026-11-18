"""Runtime configuration."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path

LAYOUTS = ("horizontal", "vertical", "blur", "split")
TITLE_MODES = ("auto", "none", "fixed")
SUBS_MODES = ("auto", "on", "off")
VERIFY_MODES = ("auto", "on", "off")
SUBS_STYLES = ("pop", "block")

# Where the streamer's webcam sits inside the source frame, as ``x:y:w:h``.
# Only the ``split`` layout uses it. ``auto`` (the default) finds it from the
# video with face detection — see ``clipper/camera.py`` — so it follows the
# streamer when they move the camera. If detection is unavailable, or you pass
# an explicit box, that box is used instead. This is the measured xQc fallback.
DEFAULT_CAM = "0:539:367:294"


def parse_cam(spec: str) -> tuple[int, int, int, int]:
    """Parse an ``x:y:w:h`` webcam box. Raises ValueError when malformed."""
    parts = [p.strip() for p in str(spec).replace(",", ":").split(":")]
    if len(parts) != 4:
        raise ValueError("expected x:y:w:h")
    try:
        x, y, w, h = (int(p) for p in parts)
    except ValueError as exc:
        raise ValueError("expected x:y:w:h integers") from exc
    if w <= 0 or h <= 0:
        raise ValueError("width and height must be positive")
    if x < 0 or y < 0:
        raise ValueError("x and y must not be negative")
    return x, y, w, h


@dataclass
class Config:
    # --- source -----------------------------------------------------------
    input: str = ""
    name: str = ""            # display/profile name; defaults to the chat channel
    loop: bool = False
    transcode: bool = False   # re-encode on ingest (needed for HEVC / odd sources)
    speed: float = 1.0        # read-rate multiplier (files only; handy for demos/test)
    segment_time: float = 2.0
    hls_list_size: int = 120  # 120 * 2s = 4 minutes of history

    # --- chat -------------------------------------------------------------
    chat: str | None = None   # twitch:<channel> | file:<path.jsonl> | sim
    chat_lag: float = 3.0     # viewers see the stream roughly this far behind ingest
    chat_window: float = 6.0
    bot_filter: bool = True

    # --- trigger ----------------------------------------------------------
    threshold: float = 0.62
    confirm: float = 1.0      # seconds the score must stay above threshold
    cooldown: float = 25.0    # minimum seconds between clip starts
    agree: int = 0            # 0 = auto (2 when chat+audio are both live), else min
    pre: float = 12.0         # seconds of lead-in
    post: float = 8.0         # seconds of tail after the trigger
    max_post: float = 18.0    # hard cap while hype keeps going
    w_chat: float = 0.55
    w_audio: float = 0.35
    w_text: float = 0.10

    # --- output -----------------------------------------------------------
    out_dir: Path = Path("clips")
    work_dir: Path = Path(".clipper")
    layout: str = "blur"
    cam: str = "auto"          # webcam box x:y:w:h, used by --layout split
    cam_interval: float = 90.0  # seconds between automatic camera re-detection
    title_mode: str = "auto"
    title: str = ""
    subs: str = "auto"        # auto | on | off — burn in speech subtitles
    subs_style: str = "pop"   # pop | block — popping word-chunks, or a text block
    subs_model: str | None = None
    subs_language: str = "en"
    subs_prompt: str | None = None   # vocabulary bias, e.g. streamer + game names
    subs_threads: int = 0     # 0 = auto (half the cores, capped at 4)
    verify: str = "auto"      # auto | on | off — verify candidates with a fast transcript
    verify_interval: float = 15.0   # minimum seconds between verifications
    verify_window: float = 10.0     # seconds of audio to transcribe per verification
    encoder: str = "auto"
    preset: str = "veryfast"
    crf: int = 20
    fps: int | None = None

    # --- operator console -------------------------------------------------
    dashboard: bool = False
    port: int = 8756
    profiles: bool = True     # save/load per-streamer tuning at <work>/profiles

    # --- misc -------------------------------------------------------------
    run_for: float = 0.0      # stop automatically after N seconds (0 = forever)
    verbose: bool = False

    def validate(self) -> None:
        if not self.input:
            raise SystemExit("error: --input is required")
        if self.layout not in LAYOUTS:
            raise SystemExit(f"error: --layout must be one of {', '.join(LAYOUTS)}")
        if self.cam != "auto":
            try:
                parse_cam(self.cam)
            except ValueError as exc:
                raise SystemExit(f"error: --cam {exc}, got '{self.cam}'") from exc
        if self.title_mode not in TITLE_MODES:
            raise SystemExit(f"error: --title-mode must be one of {', '.join(TITLE_MODES)}")
        if self.subs not in SUBS_MODES:
            raise SystemExit(f"error: --subs must be one of {', '.join(SUBS_MODES)}")
        if self.subs_style not in SUBS_STYLES:
            raise SystemExit(f"error: --subs-style must be one of {', '.join(SUBS_STYLES)}")
        if self.verify not in VERIFY_MODES:
            raise SystemExit(f"error: --verify must be one of {', '.join(VERIFY_MODES)}")
        if not 0.0 < self.threshold < 1.0:
            raise SystemExit("error: --threshold must be between 0 and 1")
        if self.agree < 0:
            raise SystemExit("error: --agree must be >= 0")
        if self.pre < 0 or self.post < 0:
            raise SystemExit("error: --pre/--post must be >= 0")
        if self.speed <= 0:
            raise SystemExit("error: --speed must be > 0")
        if self.post > self.max_post:
            self.max_post = self.post

    def as_dict(self) -> dict:
        out = {}
        for f in fields(self):
            value = getattr(self, f.name)
            out[f.name] = str(value) if isinstance(value, Path) else value
        return out


PRESETS = {
    "default": {},
    "sensitive": {"threshold": 0.50, "confirm": 0.6, "cooldown": 18.0, "pre": 10.0, "post": 7.0},
    "conservative": {"threshold": 0.74, "confirm": 1.5, "cooldown": 40.0, "pre": 16.0, "post": 10.0},
}


def apply_preset(cfg: Config, name: str) -> Config:
    for key, value in PRESETS.get(name, {}).items():
        setattr(cfg, key, value)
    return cfg
