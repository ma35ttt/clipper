"""Small shared helpers: subprocess wrappers, ffprobe, formatting, fonts."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")


def clamp01(v: float) -> float:
    return 0.0 if v < 0.0 else 1.0 if v > 1.0 else float(v)


def run(cmd: list[str], timeout: float | None = 120.0, check: bool = False):
    """Run a command without a shell. Returns CompletedProcess with text output."""
    p = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )
    if check and p.returncode != 0:
        tail = (p.stderr or "").strip().splitlines()[-8:]
        raise RuntimeError(f"command failed ({p.returncode}): {' '.join(cmd[:4])}...\n" + "\n".join(tail))
    return p


def have(binary: str) -> bool:
    return shutil.which(binary) is not None


def probe(path: str | Path) -> dict:
    """Return {width, height, fps, duration, vcodec, acodec} for a media file."""
    p = run(
        [
            FFPROBE, "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
        ],
        timeout=30,
    )
    if p.returncode != 0:
        return {}
    import json

    try:
        data = json.loads(p.stdout or "{}")
    except Exception:
        return {}
    info: dict = {"duration": 0.0, "width": 0, "height": 0, "fps": 0.0,
                  "vcodec": "", "acodec": ""}
    fmt = data.get("format") or {}
    try:
        info["duration"] = float(fmt.get("duration") or 0.0)
    except (TypeError, ValueError):
        pass
    for st in data.get("streams") or []:
        kind = st.get("codec_type")
        if kind == "video" and not info["width"]:
            info["width"] = int(st.get("width") or 0)
            info["height"] = int(st.get("height") or 0)
            info["vcodec"] = st.get("codec_name") or ""
            rate = st.get("avg_frame_rate") or "0/1"
            try:
                num, den = rate.split("/")
                info["fps"] = float(num) / float(den) if float(den) else 0.0
            except (ValueError, ZeroDivisionError):
                info["fps"] = 0.0
        elif kind == "audio" and not info["acodec"]:
            info["acodec"] = st.get("codec_name") or ""
    return info


def tail_file(path: Path, lines: int = 12) -> str:
    try:
        with open(path, "r", errors="replace") as fh:
            data = fh.readlines()
        return "".join(data[-lines:]).strip()
    except OSError:
        return ""


def fmt_clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


def safe_name(text: str, limit: int = 40) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", (text or "").strip())
    text = re.sub(r"_+", "_", text).strip("_.")
    return text[:limit] or "clip"


FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]


def find_font() -> str | None:
    override = os.environ.get("CLIPPER_FONT")
    if override and Path(override).exists():
        return override
    for cand in FONT_CANDIDATES:
        if Path(cand).exists():
            return cand
    return None


@lru_cache(maxsize=1)
def available_encoders() -> set[str]:
    p = run([FFMPEG, "-hide_banner", "-encoders"], timeout=30)
    text = p.stdout or ""
    return {line.split()[1] for line in text.splitlines()
            if line.startswith(" V") and len(line.split()) > 1}


def pick_encoder(preference: str = "auto") -> str:
    """Resolve 'auto' to hardware H.264 on macOS, else libx264."""
    if preference and preference != "auto":
        return preference
    encoders = available_encoders()
    if sys.platform == "darwin" and "h264_videotoolbox" in encoders:
        return "h264_videotoolbox"
    return "libx264"
