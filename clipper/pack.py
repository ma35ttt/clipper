"""Turn finished clips into a posting queue.

The clip sidecar already holds everything a post needs: the hook, what was said,
what chat said, and how the clip was built. This module turns that into one CSV
row per clip — a title, two alternates, a description and hashtags — so publishing
is a five-minute batch instead of an afternoon of copywriting.

It never invents a title: every variant is either the hook the renderer already
chose, a line the clip actually contains, or the phrase chat repeated. When those
are missing the row falls back to the channel name.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

FIELDS = ("file", "title", "alt_title", "alt_title_2", "description",
          "hashtags", "channel", "seconds", "structure", "voice",
          "created_at", "posted")


def _sentence(text: str) -> str:
    """A title-shaped string: sentence case, no shouting, no stray spaces."""
    text = " ".join(str(text or "").split())
    if not text:
        return ""
    letters = [ch for ch in text if ch.isalpha()]
    shouting = sum(1 for ch in letters if ch.isupper()) / max(1, len(letters))
    if shouting > 0.7:
        text = text.lower()
        text = text[0].upper() + text[1:]
    return text[:95]


def _quote(transcript: str) -> str:
    text = " ".join(str(transcript or "").split())
    if not text:
        return ""
    words = text.split()[:12]
    quoted = " ".join(words).strip(" ,.;:")
    return f'"{quoted}"' if len(quoted) > 8 else ""


def _titles(meta: dict, channel: str) -> list[str]:
    hook = _sentence(meta.get("title", ""))
    said = _quote(meta.get("transcript", ""))
    phrase = ""
    for item in meta.get("chat_excerpt", []) or []:
        text = _sentence(item.get("text", "")) if isinstance(item, dict) else ""
        if 2 <= len(text.split()) <= 6:
            phrase = text
            break

    options: list[str] = []
    if hook:
        options.append(hook)
    if said:
        options.append(f"{channel} said {said}".strip())
        options.append(said)
    if phrase:
        options.append(f'Chat: "{phrase}"')
    if not options:
        options.append(f"{channel} clip" if channel else "stream clip")
    unique: list[str] = []
    for option in options:
        option = option.strip()
        if option and option not in unique:
            unique.append(option)
    while len(unique) < 3:
        unique.append(unique[0])
    return unique[:3]


def _hashtags(channel: str) -> str:
    tags = ["#shorts", "#twitch", "#clips"]
    clean = "".join(ch for ch in (channel or "") if ch.isalnum())
    if clean:
        tags.append(f"#{clean}")
    return " ".join(tags)


def _description(meta: dict, channel: str) -> str:
    said = " ".join(str(meta.get("transcript", "")).split())
    line = said[:140].rstrip(" ,.;:")
    who = f"{channel}'s stream" if channel else "the stream"
    body = f"From {who}."
    if line:
        body = f'"{line}"\n\nFrom {who}.'
    return body


def _row(media: Path, meta: dict, channel: str) -> dict:
    titles = _titles(meta, channel)
    name = meta.get("channel") or channel
    structure = "cold open" if (meta.get("edit") or {}).get("applied") else "straight"
    voice = (meta.get("voiceover") or {}).get("engine") or "none"
    return {
        "file": media.name,
        "title": titles[0],
        "alt_title": titles[1],
        "alt_title_2": titles[2],
        "description": _description(meta, name),
        "hashtags": _hashtags(name),
        "channel": name,
        "seconds": meta.get("duration_s", ""),
        "structure": structure,
        "voice": voice,
        "created_at": meta.get("created_at", ""),
        "posted": "",
    }


def build(out_dir: Path, channel: str = "") -> tuple[Path, int]:
    """Write ``posting-queue.csv`` beside the clips. Returns (path, rows)."""
    out_dir = Path(out_dir)
    rows: list[dict] = []
    for sidecar in sorted(out_dir.glob("*.json")):
        media = sidecar.with_suffix(".mp4")
        if not media.exists():
            continue
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue
        name = str(meta.get("channel") or channel or "")
        rows.append(_row(media, meta, name))
    rows.sort(key=lambda row: str(row.get("created_at") or ""))

    target = out_dir / "posting-queue.csv"
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(FIELDS))
        writer.writeheader()
        writer.writerows(rows)
    return target, len(rows)