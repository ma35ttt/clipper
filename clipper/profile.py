"""Per-streamer tuning: the settings the console shows, saved per channel.

Every knob the website exposes is described once, here, by a :class:`Field`.
That single spec renders the settings panel, coerces whatever the browser sends
back, and decides what gets written to a per-streamer profile — so the CLI, the
saved file and the website cannot drift apart.

A profile lives at ``<work_dir>/profiles/<channel>.json`` and is loaded once at
startup. It only fills in settings still sitting at their :class:`Config`
default, so anything you passed on the command line wins over the saved file.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, fields
from pathlib import Path

from . import __version__
from .config import (LAYOUTS, SUBS_MODES, SUBS_STYLES, TITLE_MODES,
                     VERIFY_MODES, Config, parse_cam)

# "Auto" is the do-everything button. It is a bundle of field values rather than
# a hidden mode, so switching it on simply applies them and switching it off
# hands you back exactly what you had. The numbers mirror the tuned xQc run.
AUTO = {
    "layout": "split",
    "cam": "auto",
    "subs": "on",
    "subs_style": "pop",
    "title_mode": "auto",
    "verify": "on",
    "threshold": 0.58,
    "confirm": 0.8,
    "cooldown": 30.0,
    "pre": 14.0,
    "post": 10.0,
    "max_post": 25.0,
}

# Proper nouns are what whisper gets wrong most often, and a short prompt beats
# a long phrase list: long prompts get echoed back on short or noisy audio.
STREAMER_VOCAB = {
    "xqc": "xQc, Juicer, Kick, Twitch, GTA, slots, ranked",
    "kaicenat": "Kai Cenat, Twitch, AMP, Mafiathon",
    "ishowspeed": "IShowSpeed, YouTube, Ronaldo, Sidemen",
}


def autoconfigure(channel: str = "", source: str = "") -> tuple[dict, str]:
    """A one-shot "best settings for this streamer" preset.

    Starts from :data:`AUTO` (the tuned xQc setup) and adapts the parts that
    genuinely depend on the channel: the decoder vocabulary, and the trigger
    weights when the platform has no chat to read. Returns ``(values, note)``.
    """
    values = dict(AUTO)
    name = (channel or "").strip()
    key = re.sub(r"[^a-z0-9]+", "", name.lower())
    src = (source or "").lower()
    if "youtube" in src or "youtu.be" in src:
        platform = "YouTube"
    elif "twitch" in src:
        platform = "Twitch"
    else:
        platform = ""

    vocab = STREAMER_VOCAB.get(key)
    if not vocab:
        bits = [name] if name and key not in ("stream", "live", "default", "console") else []
        if platform:
            bits.append(platform)
        vocab = ", ".join(bits)

    values.update({
        "subs_prompt": vocab or None,
        "subs_model": None,      # auto: use the most accurate installed model
        "subs_threads": 4,
        "encoder": "auto",
        "agree": 0,              # let the engine use whichever signals are live
    })
    if platform == "YouTube":
        # YouTube has no chat source, so audio and keywords have to carry it.
        values.update({"w_chat": 0.0, "w_audio": 0.60, "w_text": 0.40})

    label = "this console" if key == "console" else (name or "this stream")
    detail = "pop captions, best installed whisper model"
    if platform == "YouTube":
        detail += ", audio-led triggers"
    return values, f"{label} preset · split frame · webcam follow · {detail}"


@dataclass(frozen=True)
class Field:
    """One tunable, and everything the console needs to render it."""

    key: str
    label: str
    group: str
    kind: str                    # float | int | bool | choice | text | cam
    lo: float | None = None
    hi: float | None = None
    step: float | None = None
    choices: tuple[str, ...] = ()
    note: str = ""
    unit: str = ""
    optional: bool = False       # blank means "unset" (None) rather than ""


# Groups render in this order in the console.
GROUP_ORDER = ("Trigger", "Frame & camera", "Captions", "Verify",
               "Encoder", "Chat", "Weights")

FIELDS: tuple[Field, ...] = (
    # --- Trigger (how eagerly a moment is clipped) -----------------------
    Field("threshold", "Hype threshold", "Trigger", "float", 0.0, 1.0, 0.01,
          note="How sure the signals must be before a clip fires."),
    Field("confirm", "Hold time", "Trigger", "float", 0.0, 20.0, 0.1, unit="s",
          note="Seconds the score must stay high before triggering."),
    Field("cooldown", "Cooldown", "Trigger", "float", 0.0, 300.0, 1.0, unit="s",
          note="Minimum gap between clips."),
    Field("pre", "Lead-in", "Trigger", "float", 0.0, 120.0, 1.0, unit="s",
          note="Seconds kept before the trigger."),
    Field("post", "Tail", "Trigger", "float", 0.0, 120.0, 1.0, unit="s",
          note="Seconds kept after the trigger."),
    Field("max_post", "Max tail", "Trigger", "float", 0.0, 180.0, 1.0, unit="s",
          note="Hard cap while the hype keeps going."),
    Field("agree", "Signals that must agree", "Trigger", "int", 0, 4, 1,
          note="0 = auto (2 when chat and audio are both live)."),

    # --- Frame & camera --------------------------------------------------
    Field("layout", "Layout", "Frame & camera", "choice", choices=LAYOUTS,
          note="split = webcam top, gameplay middle, captions below."),
    Field("cam", "Webcam box", "Frame & camera", "cam",
          note="x:y:w:h, or auto to detect and follow the streamer's face."),
    Field("cam_interval", "Re-detect every", "Frame & camera", "float",
          15.0, 600.0, 5.0, unit="s"),

    # --- Captions --------------------------------------------------------
    Field("subs", "Subtitles", "Captions", "choice", choices=SUBS_MODES),
    Field("subs_style", "Subtitle style", "Captions", "choice",
          choices=SUBS_STYLES, note="pop = word-chunks on a pill; block = lines."),
    Field("subs_model", "Whisper model", "Captions", "text", optional=True,
          note="Path to a ggml model; blank = auto."),
    Field("subs_language", "Language", "Captions", "text"),
    Field("subs_prompt", "Vocabulary bias", "Captions", "text", optional=True,
          note="Names/game terms to spell correctly, e.g. \"xQc, slots\"."),
    Field("subs_threads", "Whisper threads", "Captions", "int", -8, 16, 1,
          note="0 = auto (half the cores, max 4)."),
    Field("title_mode", "Hook caption", "Captions", "choice", choices=TITLE_MODES),
    Field("title", "Fixed hook text", "Captions", "text",
          note="Used when the hook mode is fixed."),

    # --- Verify ----------------------------------------------------------
    Field("verify", "Verify candidates", "Verify", "choice", choices=VERIFY_MODES,
          note="Cheap transcript check on promising moments."),
    Field("verify_interval", "Verify every", "Verify", "float", 2.0, 120.0, 1.0,
          unit="s"),
    Field("verify_window", "Verify window", "Verify", "float", 2.0, 30.0, 1.0,
          unit="s"),

    # --- Encoder ---------------------------------------------------------
    Field("encoder", "Encoder", "Encoder", "choice",
          choices=("auto", "libx264", "h264_videotoolbox"),
          note="auto = hardware H.264 on macOS, else libx264."),
    Field("preset", "x264 preset", "Encoder", "choice",
          choices=("ultrafast", "superfast", "veryfast", "faster", "fast",
                   "medium", "slow", "slower", "veryslow"),
          note="Speed vs quality (libx264 only)."),
    Field("crf", "Quality (CRF)", "Encoder", "int", 0, 51, 1,
          note="Lower is better and slower (libx264 only)."),

    # --- Chat ------------------------------------------------------------
    Field("chat_lag", "Viewer delay", "Chat", "float", 0.0, 30.0, 0.5, unit="s",
          note="Chat arrives this far behind the video."),
    Field("bot_filter", "Filter bots", "Chat", "bool",
          note="Ignore known bot accounts and spam."),

    # --- Weights ---------------------------------------------------------
    Field("w_chat", "Chat weight", "Weights", "float", 0.0, 1.0, 0.01),
    Field("w_audio", "Audio weight", "Weights", "float", 0.0, 1.0, 0.01),
    Field("w_text", "Keyword weight", "Weights", "float", 0.0, 1.0, 0.01),
)

BY_KEY = {f.key: f for f in FIELDS}


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off", ""):
        return False
    raise ValueError(f"expected true/false, got {value!r}")


def coerce(field: Field, value):
    """Turn a value from JSON/CLI into the field's real type. Raises ValueError."""
    if field.kind == "bool":
        return _as_bool(value)
    if field.kind in ("int", "float"):
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{field.label}: expected a number") from None
        if field.lo is not None:
            number = max(number, field.lo)
        if field.hi is not None:
            number = min(number, field.hi)
        return int(round(number)) if field.kind == "int" else float(number)
    if field.kind == "choice":
        text = str(value).strip()
        if text not in field.choices:
            raise ValueError(f"{field.label}: must be one of {', '.join(field.choices)}")
        return text
    text = "" if value is None else str(value).strip()
    if field.kind == "cam":
        if not text or text == "auto":
            return "auto"
        parse_cam(text)  # raises ValueError with a clear message
        return text
    if not text and field.optional:
        return None
    return text


def apply(cfg: Config, values: dict) -> list[str]:
    """Apply a mapping of field values to ``cfg``. Returns the keys that changed."""
    changed: list[str] = []
    for field in FIELDS:
        if field.key not in values:
            continue
        value = coerce(field, values[field.key])
        if getattr(cfg, field.key, None) != value:
            setattr(cfg, field.key, value)
            changed.append(field.key)
    return changed


def spec() -> list[dict]:
    """The console's description of every field, ready to JSON-encode."""
    return [
        {
            "key": f.key, "label": f.label, "group": f.group, "kind": f.kind,
            "lo": f.lo, "hi": f.hi, "step": f.step, "choices": list(f.choices),
            "note": f.note, "unit": f.unit,
        }
        for f in FIELDS
    ]


def values(cfg: Config) -> dict:
    """The current value of every field, ready to JSON-encode."""
    return {f.key: getattr(cfg, f.key, None) for f in FIELDS}


def defaults() -> dict:
    base = Config()
    return {f.key: getattr(base, f.key, None) for f in FIELDS}


def path_for(work_dir: Path, channel: str) -> Path:
    name = re.sub(r"[^A-Za-z0-9_-]+", "_", str(channel or "stream"))[:40] or "stream"
    return Path(work_dir) / "profiles" / f"{name}.json"


def load(path: Path) -> dict:
    """Read a saved profile's values. Returns {} when absent or unreadable."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    saved = data.get("values")
    if not isinstance(saved, dict):
        # Tolerate a bare mapping of values.
        saved = data
    return {k: v for k, v in saved.items() if k in BY_KEY}


def save(path: Path, values_map: dict, channel: str = "") -> bool:
    """Write a profile atomically. Returns False if it could not be written."""
    path = Path(path)
    payload = {
        "channel": channel,
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "clipper_version": __version__,
        "values": {k: v for k, v in values_map.items() if k in BY_KEY},
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        return False
    return True


def clear(path: Path) -> bool:
    """Delete a saved profile. Returns False if it could not be removed."""
    try:
        Path(path).unlink()
    except OSError:
        return False
    return True


def saved(path: Path) -> bool:
    return Path(path).exists()


def field_names() -> tuple[str, ...]:
    return tuple(f.name for f in fields(Config) if f.name in BY_KEY)
