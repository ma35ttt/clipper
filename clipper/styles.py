"""Editing styles, and a scanner that categorises reference reels.

The live engine is deliberately capture-only. The look is chosen afterwards,
per clip, from one of these styles. A style is just data: which caption backend
to use, how hard to punch in, whether to cold-open or narrate. That makes it
safe to apply the same preset to a whole session, and easy to add a preset
learned from a reference reel.

``scan`` measures a reference clip with ffmpeg alone — scene cuts, shot length,
opening pace — and turns those numbers into a style. It cannot copy a font or a
meme you have not sourced; it copies *pacing*, which is the part that survives
being re-used on different footage.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from .util import FFMPEG, probe, run, safe_name

CATEGORIES = ("rapid-fire", "hype", "montage", "clean", "podcast")

# Caption treatments. ``pop`` is the word-chunk stream every clipper uses;
# ``block`` is a readable cue line; ``static`` holds a whole sentence at once;
# ``word`` shows one huge word at a time (the retention-edit look).
CAPTION_MODES = ("pop", "block", "static", "word", "off")

# Where the caption sits. ``upper`` and ``middle`` are useful when the face is
# in the top half; ``seam`` is the join between camera and gameplay.
ANCHORS = ("auto", "top", "upper", "middle", "seam", "lower", "bottom")


@dataclass
class Style:
    """Everything the post-session renderer needs to know about a look."""

    name: str = "clean"
    label: str = "Clean"
    note: str = "One punch-in, popping white captions, gold hook."
    hook: bool = True
    hook_fill: str = "#FFD23F"
    hook_anchor: str = "auto"      # auto | top
    hook_size: float = 0.0         # fraction of frame width; 0 = default
    hook_fallback: bool = False    # use the opening line when no quotable hook
    captions: str = "pop"          # pop | block | static | word | off
    backend: str = "ass"           # ass | png
    caption_fill: str = "#FFFFFF"
    emphasis_fill: str = "#FFD23F"
    uppercase: bool = False        # shout the caption, the way the reels do
    beat_fill: str = ""            # karaoke: words flip to this as they are said
    caption_anchor: str = "auto"   # auto | top | upper | middle | seam | lower | bottom
    caption_size: float = 0.0      # fraction of frame width; 0 = per-mode default
    kicker: bool = False           # static sentence line alongside word captions
    kicker_anchor: str = "seam"
    kicker_size: float = 0.0
    colour_emphasis: bool = False  # colour a stressed word instead of a punch-in
    punch_count: int = 1
    punch_zoom: float = 1.12
    punch_len: float = 0.6
    cold_open: bool = False
    voice: bool = False
    category: str = "clean"

    def clean(self) -> "Style":
        """Clamp values so a hand-edited JSON cannot ask for a 4x zoom."""
        self.punch_count = max(0, min(int(self.punch_count), 3))
        self.punch_zoom = max(1.0, min(float(self.punch_zoom), 1.25))
        self.punch_len = max(0.35, min(float(self.punch_len), 0.9))
        if self.captions not in CAPTION_MODES:
            self.captions = "pop"
        if self.backend not in ("png", "ass"):
            self.backend = "png"
        if self.caption_anchor not in ANCHORS:
            self.caption_anchor = "auto"
        if self.kicker_anchor not in ANCHORS:
            self.kicker_anchor = "seam"
        if self.hook_anchor not in ("auto", "top"):
            self.hook_anchor = "auto"
        self.caption_size = max(0.0, min(float(self.caption_size), 0.16))
        self.kicker_size = max(0.0, min(float(self.kicker_size), 0.10))
        self.hook_size = max(0.0, min(float(self.hook_size), 0.12))
        if self.category not in CATEGORIES:
            self.category = "clean"
        self.name = safe_name(self.name or "style", 40) or "style"
        return self


BUILTINS: tuple[Style, ...] = (
    Style("clean", "Clean", "One punch-in, big uppercase captions, gold hook.",
          uppercase=True, beat_fill="#FFD23F", caption_size=0.112,
          punch_count=1, punch_zoom=1.10, punch_len=0.55),
    Style("hype", "Hype", "Three quick punch-ins; big uppercase captions, harder pacing.",
          uppercase=True, beat_fill="#FFD23F", caption_size=0.118,
          punch_count=3, punch_zoom=1.18, punch_len=0.5, category="hype"),
    Style("cinematic", "Cinematic", "Block captions, slow 1.1x push-ins, no hook.",
          hook=False, captions="block", punch_count=2, punch_zoom=1.10,
          punch_len=0.8, category="montage"),
    Style("podcast", "Podcast", "No punch-ins, block captions, no hook.",
          hook=False, captions="block", punch_count=0, category="podcast"),
    Style("raw", "Raw", "No captions, no hook, no punch-ins — the capture itself.",
          hook=False, captions="off", punch_count=0, category="clean"),
    # Reference-derived looks. The first four keep subtitles white, as the
    # default look does; only ``karaoke`` uses colour, because the reference
    # reels use a coloured word as the beat marker.
    Style("story", "Story", "One static white sentence at the seam; no hook, no punch-ins.",
          hook=False, captions="static", caption_anchor="seam",
          caption_size=0.050, punch_count=0, category="clean"),
    Style("story-karaoke", "Story + karaoke",
          "Static sentence at the seam, one huge word at a time over the camera.",
          hook=False, captions="word", caption_anchor="upper",
          caption_size=0.100, kicker=True, kicker_anchor="seam",
          kicker_size=0.044, punch_count=0, category="clean"),
    Style("meme", "Meme", "Three-line white caption in the lower third; no hook.",
          hook=False, captions="static", caption_anchor="lower",
          caption_size=0.052, punch_count=0, category="clean"),
    Style("reveal", "Reveal", "Persistent white title at the top, no spoken captions.",
          hook=True, hook_fill="#FFFFFF", hook_anchor="top", hook_size=0.052,
          hook_fallback=True, captions="off", punch_count=0, category="podcast"),
    Style("karaoke", "Karaoke", "One huge uppercase word at a time, coloured on the beat.",
          hook=False, captions="word", caption_anchor="middle",
          caption_size=0.115, colour_emphasis=True,
          emphasis_fill="#FF7A1A", punch_count=0,
          category="rapid-fire"),
)

BY_NAME = {style.name: style for style in BUILTINS}


def style_dir(work_dir: Path) -> Path:
    return Path(work_dir) / "styles"


def _from_dict(data: dict, name: str = "") -> Style | None:
    known = {f.name for f in fields(Style)}
    values = {k: v for k, v in data.items() if k in known}
    if name:
        values["name"] = name
    if not values:
        return None
    try:
        return Style(**values).clean()
    except (TypeError, ValueError):
        return None


def load(path: Path) -> Style | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return _from_dict(data, Path(path).stem)


def save(style: Style, work_dir: Path) -> Path:
    style.clean()
    folder = style_dir(work_dir)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{style.name}.json"
    tmp = path.with_suffix(".tmp")
    payload = asdict(style)
    payload["saved_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    return path


def resolve(name: str, work_dir: Path) -> Style:
    """Look up a built-in or a scanned/user style. Unknown names fall back."""
    key = (name or "").strip()
    if key.endswith(".json"):
        found = load(Path(key))
        if found:
            return found
        key = Path(key).stem
    if key in BY_NAME:
        return Style(**asdict(BY_NAME[key])).clean()
    found = load(style_dir(work_dir) / f"{safe_name(key, 40)}.json")
    if found:
        return found
    return Style(**asdict(BY_NAME["clean"])).clean()


def catalogue(work_dir: Path) -> list[dict]:
    """Built-ins plus anything the scanner has learned, for the console."""
    rows = [{"name": s.name, "label": s.label, "note": s.note,
             "category": s.category, "builtin": True} for s in BUILTINS]
    try:
        for path in sorted(style_dir(work_dir).glob("*.json")):
            style = load(path)
            if style and style.name not in BY_NAME:
                rows.append({"name": style.name, "label": style.label,
                             "note": style.note, "category": style.category,
                             "builtin": False})
    except OSError:
        pass
    return rows


# --------------------------------------------------------------------------- #
# reference scanning
# --------------------------------------------------------------------------- #

def scene_cuts(path: str | Path, limit: float = 180.0,
               threshold: float = 0.32) -> list[float]:
    """Timestamps where the picture changes hard, in the first ``limit`` secs."""
    args = [
        FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "info",
        "-t", f"{max(1.0, float(limit)):.1f}", "-i", str(path),
        "-an", "-vf", f"select='gt(scene,{threshold})',showinfo",
        "-f", "null", "-",
    ]
    try:
        result = run(args, timeout=180)
    except Exception:  # noqa: BLE001 - a broken reference is not fatal
        return []
    text = result.stderr or ""
    found = [float(m) for m in re.findall(r"pts_time:([0-9]+(?:\.[0-9]+)?)", text)]
    return sorted(set(round(t, 3) for t in found))


def classify(duration: float, cuts: list[float]) -> str:
    minutes = max(duration, 1.0) / 60.0
    rate = len(cuts) / minutes
    if rate >= 45:
        return "rapid-fire"
    if rate >= 22:
        return "hype"
    if rate >= 12:
        return "montage"
    if rate <= 5 and duration >= 45:
        return "podcast"
    return "clean"


def analyze(path: str | Path, limit: float = 180.0) -> dict:
    """Measure one reference clip and suggest a style derived from its pacing."""
    info = probe(path)
    duration = min(float(info.get("duration") or 0.0), float(limit))
    cuts = scene_cuts(path, limit=limit)
    category = classify(duration, cuts)
    first_three = len([t for t in cuts if t <= 3.0])
    gaps = [b - a for a, b in zip([0.0] + cuts, cuts + [duration]) if b > a]
    average_shot = (sum(gaps) / len(gaps)) if gaps else duration
    return {
        "file": Path(path).name,
        "path": str(path),
        "duration_s": round(duration, 2),
        "cuts": len(cuts),
        "cuts_per_min": round(len(cuts) / max(duration, 1.0) * 60.0, 1),
        "average_shot_s": round(average_shot, 2),
        "cuts_in_first_3s": first_three,
        "category": category,
    }


def style_for(category: str, name: str = "", label: str = "") -> Style:
    """Turn a measured category into a usable preset."""
    preset = {
        "rapid-fire": Style(punch_count=3, punch_zoom=1.22, punch_len=0.45,
                            captions="pop", category="rapid-fire"),
        "hype": Style(punch_count=2, punch_zoom=1.18, punch_len=0.55,
                      captions="pop", category="hype"),
        "montage": Style(punch_count=2, punch_zoom=1.12, punch_len=0.7,
                         captions="block", category="montage"),
        "clean": Style(punch_count=1, punch_zoom=1.12, punch_len=0.6,
                       captions="pop", category="clean"),
        "podcast": Style(hook=False, captions="block", punch_count=0,
                         category="podcast"),
    }[category]
    preset.name = name or preset.name
    preset.label = label or preset.label
    preset.note = f"Learned from a {category} reference — pacing only."
    return preset.clean()


def scan(paths: list[str | Path], work_dir: Path,
         log=lambda message: None) -> list[dict]:
    """Analyse references, save one style each, and write a small report."""
    rows: list[dict] = []
    for path in paths:
        path = Path(path)
        if not path.exists() or not path.is_file():
            continue
        log(f"style: measuring {path.name}")
        row = analyze(path)
        style = style_for(row["category"], name=f"ref-{safe_name(path.stem, 32)}",
                          label=f"Reference · {path.stem[:28]}")
        saved = save(style, work_dir)
        row["style"] = saved.stem
        rows.append(row)
        log(f"style: {path.name} → {row['category']} "
            f"({row['cuts_per_min']}/min, {row['average_shot_s']}s shots)")
    folder = style_dir(work_dir)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "report.json").write_text(
        json.dumps({"scanned_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "references": rows}, indent=2), encoding="utf-8")
    return rows