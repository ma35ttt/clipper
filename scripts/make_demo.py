#!/usr/bin/env python3
"""Generate a synthetic demo stream (video + audio spikes + chat replay).

    python3 scripts/make_demo.py            # writes demo/demo.mp4 + demo/chat.jsonl
    python3 -m clipper run -i demo/demo.mp4 --loop -c file:demo/chat.jsonl

Excitement happens at 38s and 66s, which is where the clip machine should fire.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "demo"
DURATION = 90
# Late enough that the rolling audio baseline has warmed up (it needs ~24s of
# history before novelty means anything).
LOUD = [(38, 46), (66, 74)]

ENABLE = "+".join(f"between(t,{a},{b})" for a, b in LOUD)


def build_video() -> Path:
    path = OUT / "demo.mp4"
    # 700 Hz sits inside the speech band the detector measures, so the synthetic
    # spike is actually visible to it (a 180 Hz rumble would be filtered out).
    audio = (
        "sine=frequency=700:sample_rate=48000:duration="
        f"{DURATION},volume=0.03,"
        f"volume=volume=14:enable='{ENABLE}'"
    )
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate=30:duration={DURATION}",
        "-f", "lavfi", "-i", audio,
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-g", "60", "-keyint_min", "60", "-sc_threshold", "0",
        "-c:a", "aac", "-b:a", "128k", "-shortest",
        str(path),
    ]
    subprocess.run(cmd, check=True)
    return path


def build_chat() -> Path:
    path = OUT / "chat.jsonl"
    rows = []
    t = 0.0
    quiet = ["nice", "lol", "ok", "true", "haha", "hi chat", "gg"]
    hype = ["NO WAY", "WHAT", "LETS GO", "OMG", "KEKW", "CLIP IT",
            "INSANE", "HOW", "BRO", "COOKED"]
    index = 0
    while t < DURATION:
        hot = any(a <= t <= b for a, b in LOUD)
        if hot:
            # A real moment looks like *many* users posting the *same* thing, so
            # repeat a few tokens across distinct viewers rather than one word
            # each.
            for i in range(9):
                rows.append({"t": round(t + i * 0.35, 1), "text": hype[i % 3],
                             "user": f"viewer{index % 400}"})
                index += 1
            t += 6.0
        else:
            rows.append({"t": round(t, 1), "text": quiet[index % len(quiet)],
                         "user": "lurker"})
            index += 1
            t += 3.5
    rows.sort(key=lambda r: r["t"])
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    video = build_video()
    chat = build_chat()
    print(f"video → {video}")
    print(f"chat  → {chat}")
    print("\ntry it:")
    print(f"  python3 -m clipper run -i {video.relative_to(ROOT)} --loop "
          f"-c file:{chat.relative_to(ROOT)} --layout blur")
    return 0


if __name__ == "__main__":
    sys.exit(main())
