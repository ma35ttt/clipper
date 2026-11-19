"""Local operator console: live score, signals, tuning, recording, clip wall.

One page, served over stdlib HTTP, no dependencies. Beside the live telemetry it
is the control panel: every tunable setting (rendered from ``clipper.profile``),
an **Auto** button that configures everything at once, a **REC** button that
opens and closes a manual recording, per-streamer profiles, camera detection and
clip housekeeping — all talking to the engine over small JSON endpoints.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .util import FFMPEG, probe, run, safe_name

# Recordings dropped on the dashboard are streamed straight to disk, so the
# usual 1 MB JSON limit does not apply. These are the containers ffmpeg can
# open on every platform clipper supports.
UPLOAD_EXTS = frozenset({
    ".mp4", ".m4v", ".mov", ".mkv", ".webm", ".ts", ".m2ts", ".flv",
    ".avi", ".mpg", ".mpeg", ".wmv",
})
UPLOAD_LIMIT = 32 * 1024 ** 3      # 32 GB is more than a 12-hour recording
UPLOAD_RESERVE = 1 * 1024 ** 3     # keep a gigabyte free after the write
_IMPORT_LOCK = threading.Lock()    # one drop lands on the wall at a time


def upload_name(raw: str) -> tuple[str, str]:
    """Split a dropped filename into a safe stem and a known extension.

    The browser supplies only ``File.name``, but it is still user input: strip
    any directory part, refuse formats ffmpeg cannot read, and flatten the stem
    so it is safe as a filename and as an output folder.
    """
    name = Path(str(raw or "").replace("\\", "/")).name
    suffix = Path(name).suffix.lower()
    if suffix not in UPLOAD_EXTS:
        pretty = suffix or "no extension"
        raise ValueError(
            f"cannot clip {pretty} files — drop an MP4, MOV, MKV, WebM, TS or AVI")
    raw_stem = Path(name).stem
    stem = safe_name(raw_stem, 60) if raw_stem.strip(" ._-") else "recording"
    return stem, suffix


def free_clip_name(out_dir: Path, stem: str) -> Path:
    """A clip filename that does not collide with an existing clip or sidecar."""
    path = out_dir / f"{stem}.mp4"
    index = 2
    while path.exists() or path.with_suffix(".json").exists():
        path = out_dir / f"{stem}-{index}.mp4"
        index += 1
    return path


def normalize_clip(raw: Path, out: Path, info: dict) -> str:
    """Put an uploaded clip in an MP4 the browser and the styler can both use.

    H.264/AAC is remuxed (a few seconds, no quality loss). Anything else is
    re-encoded once, because the clip wall plays MP4 and the styling pass looks
    for ``.mp4`` files. Returns an empty string on success, or ffmpeg's last
    error line.
    """
    copy_safe = (str(info.get("vcodec") or "").lower() == "h264"
                 and str(info.get("acodec") or "").lower() in ("", "aac", "mp3"))
    if copy_safe:
        result = run(
            [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(raw), "-map", "0:v:0", "-map", "0:a:0?",
             "-c", "copy", "-sn", "-dn", "-movflags", "+faststart",
             "-f", "mp4", str(out)],
            timeout=1800,
        )
        if result.returncode == 0 and out.exists() and out.stat().st_size:
            return ""
        out.unlink(missing_ok=True)
    result = run(
        [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
         "-i", str(raw), "-map", "0:v:0", "-map", "0:a:0?",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
         "-sn", "-dn", "-movflags", "+faststart", "-f", "mp4", str(out)],
        timeout=3600,
    )
    if result.returncode == 0 and out.exists() and out.stat().st_size:
        return ""
    out.unlink(missing_ok=True)
    tail = (result.stderr or "").strip().splitlines()
    return tail[-1] if tail else "ffmpeg could not convert the clip"

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>clipper — live</title>
<style>
  :root {
    color-scheme: dark;
    --bg: #0a0c11;
    --panel: #12151c;
    --panel2: #171b24;
    --line: rgba(255,255,255,.08);
    --line2: rgba(255,255,255,.14);
    --text: #eef1f7;
    --dim: #98a1b3;
    --faint: #667085;
    --hot: #ff3d68;
    --warm: #ffcf5c;
    --cool: #38e0b0;
    --blue: #6aa8ff;
    --radius: 16px;
    --radius-sm: 11px;
    --shadow: 0 12px 34px rgba(0,0,0,.35);
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0;
    color: var(--text);
    background:
      radial-gradient(900px 520px at 50% -220px, rgba(106,168,255,.10), transparent 70%),
      var(--bg);
    background-attachment: fixed;
    font: 14px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
    -webkit-font-smoothing: antialiased;
  }
  a { color: var(--blue); text-decoration: none; }
  header {
    display: flex; align-items: center; gap: 10px;
    padding: 12px clamp(14px, 3vw, 30px);
    border-bottom: 1px solid var(--line);
    position: sticky; top: 0; z-index: 10;
    background: rgba(10,12,17,.78);
    backdrop-filter: blur(18px) saturate(140%);
    -webkit-backdrop-filter: blur(18px) saturate(140%);
    flex-wrap: wrap;
  }
  .brand { display: flex; align-items: center; gap: 10px; margin-right: 2px; }
  .mark {
    width: 28px; height: 28px; border-radius: 8px;
    background: linear-gradient(135deg, var(--hot), #ff8a5c);
    display: grid; place-items: center; font-weight: 900; font-size: 14px;
    color: #fff; box-shadow: 0 6px 16px rgba(255,61,104,.32);
  }
  h1 { font-size: 14px; margin: 0; letter-spacing: .14em; text-transform: uppercase; font-weight: 800; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--cool);
         box-shadow: 0 0 10px var(--cool); animation: pulse 2s infinite; }
  .dot.off { background: var(--hot); box-shadow: none; animation: none; }
  @keyframes pulse { 50% { opacity: .35; transform: scale(.85); } }
  .live { display: flex; align-items: center; gap: 6px; color: var(--dim);
          font-size: 11.5px; letter-spacing: .08em; text-transform: uppercase; }
  .spacer { flex: 1; }
  .chip {
    border: 1px solid var(--line2); border-radius: 999px;
    padding: 4px 10px; font-size: 11px; letter-spacing: .05em;
    color: var(--dim); text-transform: uppercase; white-space: nowrap;
  }
  .btn {
    background: linear-gradient(180deg, #ff557a, var(--hot));
    color: #fff; border: 0; border-radius: 10px;
    padding: 9px 15px; font-weight: 800; letter-spacing: .05em;
    font-size: 12px; cursor: pointer; white-space: nowrap;
    box-shadow: 0 6px 16px rgba(255,61,104,.28);
    transition: transform .08s ease, box-shadow .15s ease, background .15s ease;
  }
  .btn:hover { box-shadow: 0 8px 22px rgba(255,61,104,.4); }
  .btn:active { transform: translateY(1px) scale(.99); }
  .btn:disabled { opacity: .6; cursor: progress; }
  .btn.ghost {
    background: rgba(255,255,255,.04); color: var(--text);
    border: 1px solid var(--line2); box-shadow: none;
    font-weight: 700; padding: 8px 13px;
  }
  .btn.ghost:hover { background: rgba(255,255,255,.08); box-shadow: none; }
  .btn.magic {
    background: linear-gradient(180deg, rgba(106,168,255,.22), rgba(106,168,255,.10));
    color: #d6e5ff; border: 1px solid rgba(106,168,255,.4); box-shadow: none;
    font-weight: 800; padding: 8px 13px;
  }
  .btn.magic:hover {
    background: linear-gradient(180deg, rgba(106,168,255,.32), rgba(106,168,255,.16));
    border-color: rgba(106,168,255,.7);
  }
  .btn.magic:disabled { opacity: .55; cursor: progress; }
  .btn.rec { background: rgba(255,255,255,.04); color: var(--dim);
             border: 1px solid var(--line2); box-shadow: none; }
  .btn.rec.on { background: linear-gradient(180deg, #ff557a, var(--hot)); color: #fff;
                box-shadow: 0 0 0 2px rgba(255,61,104,.3), 0 8px 22px rgba(255,61,104,.38);
                animation: recpulse 1.4s infinite; }
  @keyframes recpulse { 50% { box-shadow: 0 0 0 6px rgba(255,61,104,.1), 0 8px 22px rgba(255,61,104,.32); } }
  .btn.auto { background: rgba(255,255,255,.04); color: var(--dim);
              border: 1px solid var(--line2); box-shadow: none; }
  .btn.auto.on { background: linear-gradient(180deg, #46e6b8, var(--cool)); color: #06231b;
                 border: 0; box-shadow: 0 8px 22px rgba(56,224,176,.32); }
  body.console .top { display: none; }
  body.console #recbtn, body.console #clipbtn { display: none; }
  body.console #streamsec { border-color: rgba(106,168,255,.3); }
  main { max-width: 1240px; margin: 0 auto; padding: clamp(16px, 3vw, 30px);
         display: grid; gap: clamp(14px, 2vw, 20px); }
  .top { display: grid; grid-template-columns: 1.5fr 1fr; gap: clamp(14px, 2vw, 20px); }
  @media (max-width: 900px) { .top { grid-template-columns: 1fr; } }
  .panel {
    background: var(--panel);
    border: 1px solid var(--line); border-radius: var(--radius);
    padding: 18px 20px;
    box-shadow: var(--shadow);
  }
  .panel-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 14px; }
  h2 { font-size: 11px; letter-spacing: .14em; text-transform: uppercase; color: var(--dim);
       margin: 0; font-weight: 700; }
  h3 { font-size: 11px; letter-spacing: .12em; text-transform: uppercase; color: var(--warm);
       margin: 0; font-weight: 700; }
  .score-row { display: flex; align-items: flex-end; gap: 14px; }
  .big { font-size: clamp(44px, 6vw, 60px); line-height: .95; font-weight: 800;
         font-variant-numeric: tabular-nums; letter-spacing: -.02em; }
  .big small { font-size: .28em; color: var(--faint); font-weight: 600; letter-spacing: .02em; }
  .state { text-align: right; }
  .state .label { color: var(--faint); font-size: 10px; letter-spacing: .12em; text-transform: uppercase; }
  .state b { display: block; font-size: 13px; letter-spacing: .05em; text-transform: uppercase; }
  .state b.armed { color: var(--cool); }
  .state b.rec { color: var(--hot); }
  .state b.cool { color: var(--warm); }
  .state b.off { color: var(--faint); }
  svg.spark { width: 100%; height: 64px; display: block; margin: 8px 0 4px; overflow: visible; }
  .thr-label { display: flex; justify-content: space-between; color: var(--faint);
               font-size: 11px; font-variant-numeric: tabular-nums; }
  .signals { display: grid; gap: 12px; margin-top: 16px; }
  .sig { display: grid; grid-template-columns: 74px 1fr 58px; gap: 12px; align-items: center; }
  .sig b { font-weight: 600; font-size: 11px; color: var(--dim); letter-spacing: .08em; text-transform: uppercase; }
  .sig span { text-align: right; font-variant-numeric: tabular-nums; font-size: 12px; color: var(--dim); }
  .meter { height: 7px; background: rgba(255,255,255,.06); border-radius: 99px; overflow: hidden; }
  .meter > i { display: block; height: 100%; width: 0%; border-radius: 99px;
               background: linear-gradient(90deg, var(--blue), var(--cool));
               transition: width .16s ease; }
  .meter.hot > i { background: linear-gradient(90deg, var(--warm), var(--hot)); }
  .stats { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .stat { background: var(--panel2); border: 1px solid var(--line);
          border-radius: var(--radius-sm); padding: 12px 14px; }
  .stat .label { color: var(--faint); font-size: 10px; letter-spacing: .12em; text-transform: uppercase; }
  .stat b { display: block; margin-top: 5px; font-size: 20px; font-weight: 700;
            font-variant-numeric: tabular-nums; }
  .stat span { color: var(--dim); font-size: 11px; }
  .meta-row { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; }
  .meta-row .chip { font-size: 10px; padding: 3px 8px; }
  .recbanner {
    display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
    background: rgba(255,61,104,.1);
    border: 1px solid rgba(255,61,104,.4); border-radius: 13px;
    padding: 11px 15px; font-size: 13px; font-weight: 600;
  }
  .recbanner.warn { border-color: var(--warm); background: rgba(255,207,92,.1); }
  .recbanner .dot { animation: none; }
  .recbanner .tm { font-variant-numeric: tabular-nums; font-weight: 800; font-size: 15px; }
  .recbanner .hint { color: var(--dim); font-weight: 400; font-size: 12px; }
  .settings-tools { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-bottom: 14px; }
  .settings { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 16px; }
  .group { background: var(--panel2); border: 1px solid var(--line);
           border-radius: 13px; padding: 14px 16px; }
  .group-head { margin-bottom: 12px; }
  .fields { display: grid; gap: 13px; }
  .field { display: grid; gap: 5px; }
  .field > label { font-size: 12px; font-weight: 600; color: var(--text); }
  .field .unit { color: var(--faint); font-weight: 400; }
  .field .fnote { color: var(--faint); font-size: 10.5px; line-height: 1.35; }
  .field input[type=number], .field input[type=text], .field select {
    width: 100%; background: rgba(8,10,15,.55); border: 1px solid var(--line2);
    color: var(--text); border-radius: 9px; padding: 8px 10px; font-size: 13px;
    font-variant-numeric: tabular-nums;
  }
  .field input:focus, .field select:focus {
    outline: none; border-color: var(--blue);
    box-shadow: 0 0 0 2px rgba(106,168,255,.18);
  }
  .field.bool { grid-template-columns: auto 1fr; align-items: center; }
  .field.bool .fnote { grid-column: 1 / -1; }
  .switch { display: flex; align-items: center; gap: 9px; font-size: 12px; font-weight: 600; cursor: pointer; }
  .switch input { width: 16px; height: 16px; accent-color: var(--cool); }
  .log {
    font: 11.5px/1.55 ui-monospace, SFMono-Regular, Menlo, monospace;
    color: var(--dim); white-space: pre-wrap; max-height: 220px; overflow: auto;
    background: rgba(6,8,12,.55); border: 1px solid var(--line);
    border-radius: 12px; padding: 12px 14px; margin: 0;
  }
  .wall-head { display: flex; align-items: baseline; justify-content: space-between; margin: 2px 2px 0; }
  .wall-head .count { color: var(--dim); font-size: 12px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 16px; margin-top: 12px; }
  .clip { background: var(--panel);
          border: 1px solid var(--line); border-radius: 14px; overflow: hidden;
          transition: border-color .18s ease, transform .18s ease; }
  .clip:hover { border-color: var(--line2); transform: translateY(-2px); }
  .clip video { width: 100%; aspect-ratio: 9 / 16; background: #000; display: block; object-fit: contain; }
  .clip .meta { padding: 10px 12px; display: flex; justify-content: space-between;
                align-items: center; gap: 8px; border-top: 1px solid var(--line); }
  .clip .title { font-weight: 600; font-size: 12px; overflow: hidden;
                 text-overflow: ellipsis; white-space: nowrap; }
  .pill { border-radius: 999px; padding: 3px 9px; font-size: 10px; font-weight: 800;
          letter-spacing: .04em; background: rgba(255,207,92,.14); color: var(--warm);
          font-variant-numeric: tabular-nums; }
  .actions { display: flex; gap: 6px; padding: 0 10px 10px; }
  .iconbtn {
    flex: 1; text-align: center; text-decoration: none; cursor: pointer;
    background: rgba(255,255,255,.04); border: 1px solid var(--line);
    color: var(--dim); border-radius: 9px; padding: 6px 4px; font-size: 11px;
    font-weight: 600; white-space: nowrap;
  }
  .iconbtn:hover { color: var(--text); background: rgba(255,255,255,.08); }
  .iconbtn.danger:hover { color: #fff; background: rgba(255,61,104,.35); border-color: var(--hot); }
  .empty { color: var(--dim); border: 1px dashed var(--line2); border-radius: 14px;
           padding: 40px; text-align: center; background: rgba(255,255,255,.02); }
  .empty.small { padding: 18px; font-size: 12.5px; }
  .capturing { border-color: rgba(255,61,104,.5); box-shadow: 0 0 0 1px rgba(255,61,104,.3),
               0 18px 44px rgba(255,61,104,.14); }
  #toasts { position: fixed; right: 18px; bottom: 18px; z-index: 50;
            display: flex; flex-direction: column; gap: 8px; align-items: flex-end; }
  .toast { background: rgba(23,27,37,.9); border: 1px solid var(--line2); border-left: 3px solid var(--cool);
           color: var(--text); padding: 9px 14px; border-radius: 10px; font-size: 12.5px;
           box-shadow: 0 12px 30px rgba(0,0,0,.45); max-width: 340px;
           animation: toastin .16s ease; }
  .toast.bad { border-left-color: var(--hot); }
  @keyframes toastin { from { opacity: 0; transform: translateY(6px); } }
  .addrow { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
  .addrow input[type=text] {
    flex: 1 1 340px; min-width: 0; background: rgba(8,10,15,.55);
    border: 1px solid var(--line2); color: var(--text); border-radius: 11px;
    padding: 10px 12px; font-size: 13px;
  }
  .addrow input#nameinput { flex: 0 1 190px; }
  .addrow input:focus {
    outline: none; border-color: var(--blue);
    box-shadow: 0 0 0 2px rgba(106,168,255,.18);
  }
  .addrow .hint { color: var(--faint); font-size: 11.5px; }
  .streams { display: grid; gap: 8px; margin-top: 14px; }
  .streamrow {
    display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
    padding: 9px 12px; border: 1px solid var(--line); border-radius: 12px;
    background: var(--panel2);
  }
  .streamrow b { font-size: 13px; letter-spacing: .01em; }
  .streamrow .smeta { color: var(--dim); font-size: 12px; font-variant-numeric: tabular-nums; }
  .streamrow .iconbtn { flex: 0 0 auto; padding: 5px 10px; }
  .streamrow .chip { font-size: 9.5px; padding: 3px 8px; }
  .streamrow .st-error { color: var(--warm); font-size: 11.5px; max-width: 100%;
                         overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .vodcard { margin-top: 14px; border: 1px solid var(--line); border-radius: 13px;
             padding: 14px 16px; background: var(--panel2); }
  .vodcard .vhead { display: flex; align-items: center; gap: 9px; margin-bottom: 11px; }
  .vodcard .vrow { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
  .vodcard input[type=text] {
    flex: 1 1 320px; min-width: 0; background: rgba(8,10,15,.55);
    border: 1px solid var(--line2); color: var(--text); border-radius: 10px;
    padding: 9px 12px; font-size: 13px;
  }
  .vodcard input:focus { outline: none; border-color: var(--blue);
                         box-shadow: 0 0 0 2px rgba(106,168,255,.18); }
  .vodcard .vnote { color: var(--faint); font-size: 11.5px; margin-top: 10px; line-height: 1.4; }
  .vodcard .vnum, .vodcard .vcheck {
    display: inline-flex; align-items: center; gap: 6px; color: var(--dim);
    font-size: 11.5px; letter-spacing: .02em;
  }
  .vodcard .vnum input {
    width: 56px; background: rgba(8,10,15,.55); border: 1px solid var(--line2);
    color: var(--text); border-radius: 8px; padding: 7px 8px; font-size: 12px;
  }
  .vodcard .vcheck input { accent-color: var(--blue); }
  .dropzone {
    margin-top: 12px; padding: 16px; border: 1px dashed var(--line2);
    border-radius: 12px; background: rgba(8,10,15,.32); cursor: pointer;
    text-align: center; transition: border-color .16s ease, background .16s ease;
  }
  .dropzone:hover, .dropzone:focus-visible {
    outline: none; border-color: rgba(106,168,255,.65);
    background: rgba(106,168,255,.06);
  }
  .dropzone.drag {
    border-color: var(--blue); background: rgba(106,168,255,.11);
    box-shadow: inset 0 0 0 1px rgba(106,168,255,.35);
  }
  .dropzone .dmain { display: flex; align-items: center; justify-content: center;
                     gap: 9px; flex-wrap: wrap; font-size: 13px; color: var(--text); }
  .dropzone .dmain b { font-weight: 700; }
  .dropzone .dsub { color: var(--faint); font-size: 11.5px; }
  .dropzone .vnote { margin-top: 6px; }
  .clipdrop { margin: 10px 0 14px; padding: 13px 16px; }
  .uploadbar { height: 6px; border-radius: 99px; background: rgba(255,255,255,.07);
               overflow: hidden; margin-top: 10px; }
  .uploadbar > i { display: block; height: 100%; width: 0%; border-radius: 99px;
                   background: linear-gradient(90deg, var(--blue), var(--cool));
                   transition: width .2s ease; }
  .uploadnote { color: var(--dim); font-size: 11.5px; margin-top: 7px;
                overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .uploadnote.bad { color: var(--hot); }
  .walltools { display: flex; align-items: center; gap: 9px; flex-wrap: wrap; }
  .walltools select {
    background: var(--panel2); border: 1px solid var(--line2); color: var(--text);
    border-radius: 9px; padding: 7px 9px; font-size: 12px;
  }
  .stylestatus {
    display: flex; justify-content: space-between; gap: 12px; margin: 8px 0 2px;
    padding: 8px 11px; border: 1px solid var(--line); border-radius: 10px;
    background: var(--panel2); color: var(--dim); font-size: 12px;
  }
  .stylestatus b { color: var(--text); font-variant-numeric: tabular-nums; }
  .clip .pick { display: flex; align-items: center; gap: 7px; color: var(--dim);
                font-size: 11.5px; }
  .clip .pick input { accent-color: var(--blue); }
  .vodbar { height: 6px; border-radius: 99px; background: rgba(255,255,255,.07);
            overflow: hidden; margin-top: 11px; }
  .vodbar > i { display: block; height: 100%; width: 0%; border-radius: 99px;
                background: linear-gradient(90deg, var(--blue), var(--cool));
                transition: width .3s ease; }
  .vodstatus { display: flex; justify-content: space-between; gap: 10px;
               color: var(--dim); font-size: 12px; margin-top: 8px; }
  .vodstatus b { color: var(--text); font-weight: 700; font-variant-numeric: tabular-nums; }
  .modal-backdrop {
    position: fixed; inset: 0; z-index: 60; display: grid; place-items: center;
    padding: 20px; background: rgba(4,5,8,.64);
    backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px);
  }
  .modal {
    width: 100%; max-width: 460px; border-radius: 16px; padding: 22px;
    border: 1px solid var(--line2);
    background: var(--panel2);
    box-shadow: 0 30px 80px rgba(0,0,0,.6);
  }
  .modal h3 { margin-bottom: 9px; }
  .modal p { color: var(--dim); font-size: 13px; margin: 0 0 9px; }
  .modal p.warnline { color: var(--warm); font-size: 12px; }
  .modal-actions { display: flex; gap: 10px; justify-content: flex-end;
                   flex-wrap: wrap; margin-top: 16px; }
  .hidden { display: none !important; }
</style>
</head>
<body>
<header>
  <div class="brand"><span class="mark">c</span><h1>clipper</h1></div>
  <span class="live"><span class="dot" id="dot"></span><span id="conn">live</span></span>
  <span class="chip" id="chan">—</span>
  <span class="spacer"></span>
  <span class="chip" id="uptime">0h 00m</span>
  <button class="btn ghost" id="updbtn" onclick="updateClipper()" title="pull the newest version from GitHub">Update</button>
  <button class="btn auto" id="autobtn" onclick="toggleAuto()" title="Configure everything: 9:16 frame, webcam follow, synced captions, smart hook (a)">★ AUTO</button>
  <button class="btn rec" id="recbtn" onclick="toggleRec()" title="Start / stop a manual recording (r)">● REC</button>
  <button class="btn" id="clipbtn" onclick="snap()" title="Force a clip at this moment (c)">CLIP NOW</button>
</header>
<main>
  <div class="recbanner hidden" id="recbanner">
    <span class="dot" style="background:var(--hot);box-shadow:0 0 12px var(--hot)"></span>
    <span>RECORDING</span>
    <span class="tm" id="rectime">0.0s</span>
    <span class="hint" id="recmax"></span>
    <span class="spacer"></span>
    <span class="hint">press <b>R</b> or hit stop to cut the clip</span>
  </div>
  <section class="top">
    <div class="panel" id="scorecard">
      <div class="panel-head">
        <h2>hype score</h2>
        <div class="state">
          <span class="label">state</span>
          <b id="state" class="armed">armed</b>
        </div>
      </div>
      <div class="score-row">
        <div class="big" id="score">0.00</div>
        <div class="thr-label" style="flex:1;align-self:center;justify-content:flex-end">
          <span id="thr">trigger 0.62</span>
        </div>
      </div>
      <svg class="spark" viewBox="0 0 320 64" preserveAspectRatio="none" aria-hidden="true">
        <defs>
          <linearGradient id="sparkfill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="#ff3d68" stop-opacity=".32"/>
            <stop offset="100%" stop-color="#ff3d68" stop-opacity="0"/>
          </linearGradient>
        </defs>
        <path id="sparkarea" fill="url(#sparkfill)" d=""></path>
        <polyline id="sparkline" fill="none" stroke="#ff6b8b" stroke-width="2"
                  stroke-linejoin="round" stroke-linecap="round" points=""></polyline>
        <line id="thrline" x1="0" y1="24" x2="320" y2="24" stroke="rgba(255,207,92,.45)"
              stroke-dasharray="4 4" stroke-width="1"></line>
      </svg>
      <div class="thr-label"><span>now</span><span id="thr2">0.62</span></div>
      <div class="signals">
        <div class="sig"><b>chat</b><div class="meter"><i id="bchat"></i></div><span id="vchat">0u</span></div>
        <div class="sig"><b>audio</b><div class="meter"><i id="baudio"></i></div><span id="vaudio">—</span></div>
        <div class="sig"><b>keywords</b><div class="meter"><i id="btext"></i></div><span id="vtext">0</span></div>
        <div class="sig"><b>agree</b><div class="meter"><i id="bgate"></i></div><span id="vgate">0/1</span></div>
      </div>
    </div>
    <div class="panel">
      <div class="panel-head"><h2>session</h2><span class="chip" id="cd">ready</span></div>
      <div class="stats">
        <div class="stat"><div class="label">clips</div><b id="clips">0</b><span>this session</span></div>
        <div class="stat"><div class="label">buffer</div><b id="buffer">—</b><span>rolling window</span></div>
      </div>
      <div class="meta-row">
        <span class="chip" id="m-layout">layout —</span>
        <span class="chip" id="m-cam">cam —</span>
        <span class="chip" id="m-style">subs —</span>
        <span class="chip" id="m-enc">encoder —</span>
        <span class="chip" id="m-prof">profile —</span>
      </div>
    </div>
  </section>
  <section class="panel" id="streamsec">
    <div class="panel-head">
      <h2>add a stream</h2>
      <span class="chip" id="streamhint">twitch or youtube link</span>
    </div>
    <form class="addrow" id="addform" onsubmit="return addStream(event)">
      <input type="text" id="linkinput" placeholder="https://twitch.tv/kaicenat  ·  https://youtube.com/@handle/live"
             spellcheck="false" autocomplete="off" aria-label="stream link">
      <input type="text" id="nameinput" placeholder="name (optional)" maxlength="24"
             spellcheck="false" autocomplete="off" aria-label="streamer name">
      <button class="btn" type="submit" id="addbtn">Add stream</button>
    </form>
    <div class="streams" id="streams"><div class="empty small">only this stream is clipping</div></div>
    <div class="vodcard" id="vodcard">
      <div class="vhead"><h3>clip a past stream</h3><span class="chip" id="vodchip">vod</span></div>
      <div class="vrow">
        <input type="text" id="vodinput" spellcheck="false" autocomplete="off"
               aria-label="past stream link"
               placeholder="https://twitch.tv/videos/123456789  ·  https://youtu.be/...">
        <label class="vnum" title="how many clips this run may take">clips
          <input type="number" id="vodmax" min="1" max="60" value="12">
        </label>
        <label class="vcheck" title="ignore clips taken from this source in earlier runs">
          <input type="checkbox" id="vodreset"> start over
        </label>
        <button class="btn magic" id="vodbtn" onclick="startVod()"
                title="find the best moments in a finished stream and cut clips from it">Clip past stream</button>
      </div>
      <div class="vnote" id="vodnote">Analyses a finished stream and cuts up to 12 new moments. Run it again for the next batch — already-clipped moments are remembered per source.</div>
      <div class="vodbar hidden" id="vodbar"><i id="vodfill"></i></div>
      <div class="vodstatus hidden" id="vodstatus"><span id="vodphase">starting…</span><b id="vodcount"></b></div>
    </div>
  </section>
  <section class="panel">
    <div class="panel-head">
      <h2>tuning</h2>
      <button class="btn ghost" onclick="toggleSettings()" id="settToggle">hide settings</button>
    </div>
    <div id="setbody">
      <div class="settings-tools">
        <button class="btn magic" id="autocfg" onclick="autoConfigure()" title="pick the best settings for this streamer in one shot">Auto configure</button>
        <button class="btn ghost" onclick="profileAction('save')" title="write the profile now — every change is saved anyway">Save profile (s)</button>
        <button class="btn ghost" onclick="profileAction('load')" title="re-read the saved profile from disk">Reload</button>
        <button class="btn ghost" onclick="profileAction('reset')" title="back to defaults and forget this stream's profile">Reset</button>
        <button class="btn ghost" id="findcam" onclick="findCam()" title="run face detection now and move the webcam crop">Find camera</button>
        <span class="spacer"></span>
        <span class="chip" id="profnote">changes apply as you edit</span>
      </div>
      <div class="settings" id="settings"><div class="empty">loading settings…</div></div>
    </div>
  </section>
  <section class="panel">
    <div class="panel-head"><h2>activity</h2><span class="chip" id="logchip">live</span></div>
    <pre class="log" id="log">no activity yet</pre>
  </section>
  <section>
    <div class="wall-head">
      <h2>clips</h2>
      <div class="walltools">
        <select id="stylepick" aria-label="editing style"></select>
        <button class="btn ghost" id="stylebtn" onclick="styleSelected()"
                title="transcribe, hook and punch-in the selected capture-only clips">Style selected</button>
        <span class="count" id="count">nothing yet</span>
      </div>
    </div>
    <div class="dropzone clipdrop" id="dropzone" role="button" tabindex="0"
         aria-label="Drop raw clips here or select them from this Mac">
      <div class="dmain">
        <b>Drop raw clips here</b>
        <span class="dsub">or</span>
        <button class="btn ghost" id="localfile" type="button">Select from local</button>
        <span class="dsub">— they appear below, ready to style.</span>
      </div>
      <div class="vnote">MP4, MOV, MKV, WebM, TS or AVI · normalized to MP4 on this Mac · tick a clip and press Style selected.</div>
      <input type="file" id="fileinput" hidden multiple
             accept="video/*,.mp4,.m4v,.mov,.mkv,.webm,.ts,.m2ts,.flv,.avi,.mpg,.mpeg,.wmv">
    </div>
    <div class="uploadbar hidden" id="uploadbar"><i id="uploadfill"></i></div>
    <div class="uploadnote hidden" id="uploadnote"></div>
    <div class="stylestatus hidden" id="stylestatus"><span id="stylenote"></span><b id="stylecount"></b></div>
    <div class="grid" id="wall"></div>
  </section>
</main>
<div id="toasts"></div>
<div class="modal-backdrop hidden" id="choicemodal" role="dialog" aria-modal="true" aria-labelledby="choicetitle">
  <div class="modal">
    <h3 id="choicetitle">another stream is already clipping</h3>
    <p id="choicetext"></p>
    <p class="warnline">Running both means two ingests, two caption pipelines and two renders. On an 8 GB machine expect swap.</p>
    <div class="modal-actions">
      <button class="btn ghost" id="cancelbtn" onclick="resolveChoice('cancel')">Keep current only</button>
      <button class="btn" id="bothbtn" onclick="resolveChoice('both')">Run both</button>
    </div>
  </div>
</div>
<script>
const $ = id => document.getElementById(id);
const pct = v => (Math.max(0, Math.min(1, v)) * 100).toFixed(1) + '%';
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let lastCount = -1;
let outDir = '';
let autoOn = false;
let recording = false;
let settingsTimer = null;
let uploadBusy = false;
let uploadQueue = [];
const history = [];
const MAXH = 90;
let threshold = 0.62;

async function postJSON(url, body) {
  try {
    const r = await fetch(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body || {}),
    });
    return await r.json().catch(() => ({}));
  } catch (e) {
    toast('console unreachable', true);
    return null;
  }
}

function toast(msg, bad) {
  const el = document.createElement('div');
  el.className = 'toast' + (bad ? ' bad' : '');
  el.textContent = msg;
  $('toasts').appendChild(el);
  setTimeout(() => el.remove(), 3400);
}

/* -- actions -------------------------------------------------------- */
async function snap() {
  const r = await postJSON('/api/clip', {action: 'snap'});
  if (r && r.ok === false) toast(r.error || 'clip failed', true);
  else toast('clip requested — rendering next');
}

async function toggleRec() {
  const r = await postJSON('/api/clip', {action: 'toggle'});
  if (!r) return;
  if (r.ok === false) return toast(r.error || 'recording failed', true);
  if (r.recording) toast('recording — press R to cut');
  else toast(r.note === 'too short' ? 'recording too short, discarded' : 'recording cut');
}

async function toggleAuto() {
  const r = await postJSON('/api/auto', {on: !autoOn});
  if (!r) return;
  if (r.ok === false) return toast(r.error || 'auto failed', true);
  fillSettings(r, true);
  toast(autoOn ? 'auto on — everything configured' : 'auto off — your settings are back');
}

async function profileAction(action) {
  const r = await postJSON('/api/profile', {action});
  if (!r) return;
  if (r.ok === false) toast(r.error || (action + ' failed'), true);
  else toast('profile ' + (action === 'save' ? 'saved'
    : action === 'load' ? 'reloaded from disk' : 'reset to defaults'));
  fillSettings(r, true);
}

async function autoConfigure() {
  const btn = $('autocfg');
  btn.disabled = true;
  btn.textContent = 'configuring...';
  const r = await postJSON('/api/autoconfigure', {});
  btn.disabled = false;
  btn.textContent = 'Auto configure';
  if (!r) return;
  if (r.ok === false) return toast(r.error || 'auto configure failed', true);
  fillSettings(r, true);
  const note = $('profnote');
  if (note) note.textContent = r.note || 'auto configured';
  toast(r.note || 'auto configured');
}

async function findCam() {
  const r = await postJSON('/api/camera', {});
  if (!r) return;
  if (r.ok === false) return toast(r.error || 'camera detection failed', true);
  $('findcam').textContent = 'detecting…';
  setTimeout(() => { $('findcam').textContent = 'Find camera'; }, 4000);
  toast('looking for the streamer\\'s face…');
}

function queueApply() {
  clearTimeout(settingsTimer);
  settingsTimer = setTimeout(applySettings, 450);
}

async function applySettings() {
  const values = collect();
  const r = await postJSON('/api/settings', {values});
  if (!r) return;
  if (r.ok === false) return toast(r.error || 'could not apply', true);
  fillSettings(r, false);
}

function collect() {
  const out = {};
  document.querySelectorAll('[data-key]').forEach(el => {
    out[el.dataset.key] = el.type === 'checkbox' ? el.checked : el.value;
  });
  return out;
}

function toggleSettings() {
  const body = $('setbody');
  body.classList.toggle('hidden');
  $('settToggle').textContent = body.classList.contains('hidden')
    ? 'show settings' : 'hide settings';
}

async function delClip(file) {
  if (!confirm('Delete ' + file + ' ?')) return;
  const r = await postJSON('/api/clips/delete', {file});
  if (!r) return;
  if (r.ok === false) return toast(r.error || 'delete failed', true);
  toast('deleted');
  lastCount = -1;
  tick();
}

function copyText(text) {
  const done = () => toast('copied: ' + text);
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done, () => prompt('path', text));
  } else {
    prompt('path', text);
  }
}

function copyPath(file) {
  copyText((outDir ? outDir + '/' : '') + file);
}

function copyFolder(path) {
  copyText(path);
}

/* -- updating ------------------------------------------------------- */
async function updateClipper() {
  const btn = $('updbtn');
  btn.disabled = true; btn.textContent = 'Checking...';
  const r = await postJSON('/api/update', {action: 'apply'});
  btn.disabled = false;
  if (!r) { btn.textContent = 'Update'; return; }
  btn.textContent = r.updated ? 'Restart to apply' : 'Update';
  if (r.ok === false) return toast(r.message || r.error || 'update failed', true);
  toast(r.message || 'up to date');
}

/* -- past streams (the VOD section) --------------------------------- */
async function startVod() {
  const input = $('vodinput');
  const link = input.value.trim();
  if (!link) { toast('paste a link to a past stream first', true); return; }
  const btn = $('vodbtn');
  btn.disabled = true; btn.textContent = 'Starting...';
  const r = await postJSON('/api/vod', {
    url: link,
    name: $('nameinput').value.trim(),
    max_clips: Number($('vodmax').value || 12),
    reset: $('vodreset').checked,
  });
  btn.disabled = false; btn.textContent = 'Clip past stream';
  if (!r) return;
  if (r.ok === false) return toast(r.error || 'could not start', true);
  // Keep the link in the box: pressing the button again takes the next batch.
  toast('analysing the past stream — watch the activity log');
}

/* -- raw clips (drop a file, then style it) -------------------------- */
// Browsers cannot hand us a path, so a dropped clip is streamed to the console
// as raw bytes. The console normalizes it to MP4 and files it on the clip wall;
// it is not cut or analysed until the operator ticks it and picks a style.
function pickLocal() {
  $('fileinput').click();
}

function queueLocal(fileList) {
  const files = [...(fileList || [])];
  if (!files.length) return;
  for (const file of files) {
    if (!videoExtension(file.name)) {
      toast('cannot clip ' + file.name + ' — drop an MP4, MOV, MKV, WebM, TS or AVI', true);
      continue;
    }
    uploadQueue.push(file);
  }
  pumpLocal();
}

function videoExtension(name) {
  return /\\.(mp4|m4v|mov|mkv|webm|ts|m2ts|flv|avi|mpg|mpeg|wmv)$/i.test(name || '');
}

function uploadFile(file) {
  return new Promise(resolve => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/upload?name=' + encodeURIComponent(file.name));
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    xhr.upload.onprogress = e => {
      if (!e.lengthComputable) return;
      showUpload(file.name + ' — ' + Math.round(100 * e.loaded / e.total) + '%',
                 e.loaded / e.total);
    };
    xhr.onload = () => {
      try { resolve(JSON.parse(xhr.responseText || '{}')); }
      catch (err) { resolve({ok: false, error: 'console sent an unreadable reply'}); }
    };
    xhr.onerror = () => resolve({ok: false, error: 'upload could not reach the console'});
    xhr.onabort = () => resolve({ok: false, error: 'upload cancelled'});
    xhr.send(file);
  });
}

function showUpload(note, ratio, bad) {
  const bar = $('uploadbar'), fill = $('uploadfill'), text = $('uploadnote');
  bar.classList.toggle('hidden', ratio == null);
  text.classList.remove('hidden');
  text.classList.toggle('bad', !!bad);
  text.textContent = note;
  if (ratio != null) fill.style.width = pct(ratio);
}

function hideUploadSoon() {
  setTimeout(() => {
    if (uploadBusy || uploadQueue.length) return;
    $('uploadbar').classList.add('hidden');
    $('uploadnote').classList.add('hidden');
  }, 5000);
}

async function pumpLocal() {
  if (uploadBusy) return;
  if (!uploadQueue.length) { hideUploadSoon(); return; }
  const file = uploadQueue.shift();
  uploadBusy = true;
  showUpload('sending ' + file.name + ' to this Mac...', 0);
  const r = await uploadFile(file);
  uploadBusy = false;
  if (!r || r.ok === false) {
    showUpload((r && r.error) || 'upload failed', null, true);
    hideUploadSoon();
    pumpLocal();
    return;
  }
  const secs = r.duration || 0;
  const length = secs >= 60 ? Math.round(secs / 60) + ' min'
    : Math.max(1, Math.round(secs)) + ' s';
  showUpload('added ' + r.file + ' to the wall · ' + length, 1);
  toast('added ' + (r.title || r.file) + ' — tick it and pick a style');
  // The wall only repaints when the clip count changes.
  lastCount = -1;
  tick();
  pumpLocal();
}

const dropzone = $('dropzone');
$('fileinput').addEventListener('change', e => {
  queueLocal(e.target.files);
  e.target.value = '';
});
dropzone.addEventListener('click', e => {
  // The file input lives inside the box; ignore its own click so opening the
  // picker does not bubble back here and reopen it.
  if (e.target !== $('fileinput')) pickLocal();
});
dropzone.addEventListener('keydown', e => {
  if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pickLocal(); }
});
['dragenter', 'dragover'].forEach(name => dropzone.addEventListener(name, e => {
  e.preventDefault(); e.stopPropagation();
  dropzone.classList.add('drag');
}));
['dragleave', 'dragend'].forEach(name => dropzone.addEventListener(name, e => {
  e.preventDefault();
  dropzone.classList.remove('drag');
}));
dropzone.addEventListener('drop', e => {
  e.preventDefault(); e.stopPropagation();
  dropzone.classList.remove('drag');
  queueLocal(e.dataTransfer.files);
});
// A file dropped just outside the box must not navigate the page away.
['dragover', 'drop'].forEach(name => window.addEventListener(name, e => {
  if (!dropzone.contains(e.target)) e.preventDefault();
}));

/* -- post-session styling ------------------------------------------- */
async function styleSelected() {
  const files = [...document.querySelectorAll('.clipick:checked')].map(el => el.value);
  if (!files.length) { toast('tick the clips you want to style first', true); return; }
  const btn = $('stylebtn'), style = $('stylepick').value || 'clean';
  btn.disabled = true; btn.textContent = 'Starting...';
  const r = await postJSON('/api/style', {files, style});
  btn.disabled = false; btn.textContent = 'Style selected';
  if (!r) return;
  if (r.ok === false) return toast(r.error || 'could not start', true);
  toast('styling ' + files.length + ' clip(s) — captions and punch-ins take a minute');
}

function renderStyles(list) {
  const pick = $('stylepick');
  if (!pick || !list) return;
  const current = pick.value;
  pick.innerHTML = list.map(s =>
    `<option value="${esc(s.name)}">${esc(s.label)}</option>`).join('');
  if (current && list.some(s => s.name === current)) pick.value = current;
}

function renderStyleJob(v) {
  const box = $('stylestatus'), note = $('stylenote'), count = $('stylecount');
  if (!v || (!v.running && !v.phase)) { box.classList.add('hidden'); return; }
  box.classList.remove('hidden');
  if (v.running) {
    note.textContent = (v.style ? v.style + ' · ' : '') + 'styling ' + (v.file || '...');
    count.textContent = (v.rendered || 0) + ' / ' + (v.total || 0);
  } else if (v.error) {
    note.textContent = 'styling failed: ' + v.error;
    count.textContent = '';
  } else {
    note.textContent = 'styling finished — ' + (v.rendered || 0) + ' clip(s) added';
    count.textContent = '';
    setTimeout(() => box.classList.add('hidden'), 6000);
  }
}

async function stopVod() {
  const r = await postJSON('/api/vod/stop', {});
  if (!r) return;
  if (r.ok === false) return toast(r.error || 'nothing to stop', true);
  toast(r.note || 'stopping');
}

const VOD_PHASE = {
  starting: 'starting', resolving: 'resolving the link', chat: 'reading the chat',
  analysing: 'measuring the audio', rendering: 'rendering clips',
  done: 'finished', error: 'failed',
};

function renderVod(v) {
  const bar = $('vodbar'), status = $('vodstatus'), phase = $('vodphase');
  const count = $('vodcount'), note = $('vodnote'), chip = $('vodchip');
  if (!v || (!v.running && !v.phase)) {
    bar.classList.add('hidden'); status.classList.add('hidden');
    chip.textContent = 'vod'; chip.style.color = '';
    return;
  }
  bar.classList.remove('hidden'); status.classList.remove('hidden');
  const total = v.total || 0, done = v.rendered || 0;
  $('vodfill').style.width = total ? pct(Math.min(1, done / total))
    : (v.running ? '35%' : '100%');
  const label = VOD_PHASE[v.phase] || v.phase || 'working';
  phase.textContent = (v.name ? v.name + ' · ' : '') + label + (v.running ? '…' : '');
  count.textContent = total ? done + ' / ' + total + ' clips'
    : (v.found ? v.found + ' moment' + (v.found === 1 ? '' : 's') : '');
  if (v.running) {
    chip.textContent = total ? 'rendering' : 'analysing';
    chip.style.color = 'var(--blue)';
  } else if (v.error) {
    chip.textContent = 'failed'; chip.style.color = 'var(--hot)';
  } else {
    chip.textContent = 'done'; chip.style.color = 'var(--cool)';
  }
  if (v.error) {
    note.textContent = 'error: ' + v.error;
    note.style.color = 'var(--hot)';
  } else {
    note.textContent = 'Cuts up to ' + (($('vodmax') && $('vodmax').value) || 12)
      + ' new moments. Run it again for the next batch; the ledger per source '
      + 'stops it repeating clips.';
    note.style.color = '';
  }
}


/* -- extra streams (the link section) ------------------------------- */
let pendingStream = null;
let streamSig = '';

function statusText(st) {
  if (st.status === 'live') return 'clipping';
  if (st.status === 'starting') return 'starting…';
  if (st.status === 'failed') return 'failed';
  return 'stopped';
}

function renderStreams(s) {
  const list = s.streams || [];
  const sig = [s.channel, !!s.connected, (s.score || 0).toFixed(2), s.clips.length]
    .concat(list.map(x => [x.name, x.status, x.clips, (x.score || 0).toFixed(2),
                           x.error || ''].join(':'))).join('|');
  if (sig === streamSig) return;
  streamSig = sig;
  const rows = [];
  if (!s.console) {
    rows.push(`
    <div class="streamrow">
      <span class="dot ${s.connected ? '' : 'off'}"></span>
      <b>${esc(s.channel || 'this stream')}</b>
      <span class="chip">main</span>
      <span class="smeta">${(s.score || 0).toFixed(2)} · ${s.clips.length} clip${s.clips.length === 1 ? '' : 's'} · ${esc(s.layout || '')}</span>
      <span class="spacer"></span>
      <span class="smeta">${s.connected ? 'clipping' : 'offline'}</span>
    </div>`);
  }
  if (!list.length && s.console) {
    rows.push('<div class="empty small">no streams yet — paste a link above to start clipping</div>');
  }
  for (const st of list) {
    const live = st.status === 'live';
    const dead = st.status === 'failed' || st.status === 'stopped';
    rows.push(`
      <div class="streamrow">
        <span class="dot ${live ? '' : 'off'}"></span>
        <b>${esc(st.name)}</b>
        <span class="chip">${esc(st.kind || 'stream')}</span>
        <span class="smeta">${(st.score || 0).toFixed(2)} · ${st.clips} clip${st.clips === 1 ? '' : 's'} · ${statusText(st)}</span>
        <span class="spacer"></span>
        ${live ? `<a class="iconbtn" href="http://127.0.0.1:${st.port}/" target="_blank" rel="noopener">open</a>` : ''}
        <button class="iconbtn" onclick="copyFolder(this.dataset.path)" data-path="${esc(st.out_dir)}">copy path</button>
        ${dead
          ? `<button class="iconbtn" onclick="retryStream(this.dataset.name)" data-name="${esc(st.name)}">retry</button>`
          : `<button class="iconbtn danger" onclick="stopStream(this.dataset.name)" data-name="${esc(st.name)}">stop</button>`}
        ${st.error ? `<span class="st-error" title="${esc(st.error)}">${esc(st.error)}</span>` : ''}
      </div>`);
  }
  $('streams').innerHTML = rows.join('');
}

async function addStream(e) {
  if (e) e.preventDefault();
  const url = $('linkinput').value.trim();
  const name = $('nameinput').value.trim();
  if (!url) { toast('paste a twitch or youtube link first', true); return false; }
  const btn = $('addbtn');
  btn.disabled = true; btn.textContent = 'adding…';
  const r = await postJSON('/api/streams', {url, name, mode: 'ask'});
  btn.disabled = false; btn.textContent = 'Add stream';
  if (!r) return false;
  if (r.ok === false) { toast(r.error || 'could not add the stream', true); return false; }
  if (r.needs_choice) { askBoth(r); return false; }
  afterAdd(r);
  return false;
}

function askBoth(r) {
  const t = r.target || {};
  pendingStream = {url: t.input || $('linkinput').value.trim(), name: t.name || ''};
  const target = t.name || 'the new stream';
  const busy = (r.busy || []).join(', ') || 'another stream';
  $('choicetext').textContent =
    'You are already clipping ' + busy + '. Run ' + target + ' at the same time?';
  $('choicemodal').classList.remove('hidden');
  $('bothbtn').focus();
}

async function resolveChoice(mode) {
  $('choicemodal').classList.add('hidden');
  const pending = pendingStream;
  pendingStream = null;
  if (!pending) return;
  if (mode !== 'both') { toast('kept the current stream only'); return; }
  const r = await postJSON('/api/streams', {...pending, mode: 'both'});
  if (!r) return;
  if (r.ok === false) return toast(r.error || 'could not add the stream', true);
  afterAdd(r);
}

function afterAdd(r) {
  $('linkinput').value = '';
  $('nameinput').value = '';
  const st = r.stream || {};
  const path = (st.out_dir || '').split('/').filter(Boolean).slice(-2).join('/');
  toast((st.name || 'stream') + ' added — clips go to ' + (path || 'its own folder'));
  streamSig = '';
  tick();
}

async function stopStream(name) {
  if (!confirm('Stop clipping ' + name + '? Its clips stay on disk.')) return;
  const r = await postJSON('/api/streams/stop', {name});
  if (!r) return;
  if (r.ok === false) return toast(r.error || 'could not stop it', true);
  toast(name + ' stopped');
  streamSig = '';
  tick();
}

async function retryStream(name) {
  const r = await postJSON('/api/streams/retry', {name});
  if (!r) return;
  if (r.ok === false) return toast(r.error || 'could not restart it', true);
  toast(name + ' restarting');
  streamSig = '';
  tick();
}

/* -- settings panel ------------------------------------------------- */
function fieldEl(f) {
  const wrap = document.createElement('div');
  wrap.className = 'field' + (f.kind === 'bool' ? ' bool' : '');
  const id = 'set-' + f.key;
  if (f.kind === 'bool') {
    wrap.innerHTML = `<label class="switch"><input type="checkbox" id="${id}" data-key="${f.key}"><span>${esc(f.label)}</span></label>`;
  } else if (f.kind === 'choice') {
    wrap.innerHTML = `<label for="${id}">${esc(f.label)}</label>` +
      `<select id="${id}" data-key="${f.key}">` +
      f.choices.map(c => `<option value="${esc(c)}">${esc(c)}</option>`).join('') +
      '</select>';
  } else {
    const type = (f.kind === 'text' || f.kind === 'cam') ? 'text' : 'number';
    const attrs = type === 'number'
      ? ` step="${f.step == null ? 1 : f.step}" min="${f.lo == null ? 0 : f.lo}" max="${f.hi == null ? 1000000 : f.hi}"`
      : ' spellcheck="false" autocomplete="off"';
    const unit = f.unit ? ` <span class="unit">${esc(f.unit)}</span>` : '';
    wrap.innerHTML = `<label for="${id}">${esc(f.label)}${unit}</label>` +
      `<input type="${type}" id="${id}" data-key="${f.key}"${attrs}>`;
  }
  if (f.note) {
    const note = document.createElement('div');
    note.className = 'fnote';
    note.textContent = f.note;
    wrap.appendChild(note);
  }
  // Text needs no commit to be safe; numbers/choices apply when you leave the field.
  if (f.kind === 'text' || f.kind === 'cam') wrap.addEventListener('input', queueApply);
  wrap.addEventListener('change', queueApply);
  return wrap;
}

function buildSettings(spec, groups) {
  const host = $('settings');
  host.innerHTML = '';
  for (const g of (groups || [])) {
    const card = document.createElement('div');
    card.className = 'group';
    const head = document.createElement('div');
    head.className = 'group-head';
    head.innerHTML = `<h3>${esc(g)}</h3>`;
    card.appendChild(head);
    const body = document.createElement('div');
    body.className = 'fields';
    spec.filter(f => f.group === g).forEach(f => body.appendChild(fieldEl(f)));
    card.appendChild(body);
    host.appendChild(card);
  }
}

function fillSettings(data, force) {
  if (!data || !data.spec) return;
  for (const f of data.spec) {
    const el = document.querySelector(`[data-key="${f.key}"]`);
    if (!el) continue;
    if (!force && document.activeElement === el) continue;
    const v = data.values ? data.values[f.key] : undefined;
    if (f.kind === 'bool') el.checked = !!v;
    else el.value = (v === null || v === undefined) ? '' : v;
  }
  setAuto(!!data.auto);
  const chip = $('m-prof');
  if (chip) chip.textContent = data.profiles === false ? 'profiles off'
    : (data.profile_saved ? 'profile saved' : 'profile —');
  const note = $('profnote');
  if (note) note.textContent = data.profiles === false
    ? 'profiles disabled (--no-profile) — changes apply to this run only'
    : 'changes apply as you edit';
}

function setAuto(on) {
  autoOn = on;
  const b = $('autobtn');
  b.classList.toggle('on', on);
  b.textContent = on ? '★ AUTO ON' : '★ AUTO';
}

/* -- telemetry ------------------------------------------------------ */
function drawSpark() {
  const w = 320, h = 64, n = history.length;
  if (!n) { $('sparkline').setAttribute('points',''); $('sparkarea').setAttribute('d',''); return; }
  const step = w / (MAXH - 1);
  const pts = history.map((v, i) => [i * step, h - Math.max(0, Math.min(1, v)) * h]);
  $('sparkline').setAttribute('points',
    pts.map(p => p[0].toFixed(1) + ',' + p[1].toFixed(1)).join(' '));
  const area = pts.map((p, i) => (i ? 'L' : 'M') + p[0].toFixed(1) + ' ' + p[1].toFixed(1)).join(' ') +
               ' L' + ((n - 1) * step).toFixed(1) + ' ' + h + ' L0 ' + h + ' Z';
  $('sparkarea').setAttribute('d', area);
  const y = h - threshold * h;
  $('thrline').setAttribute('y1', y); $('thrline').setAttribute('y2', y);
}

function setState(text, cls) {
  const el = $('state');
  el.textContent = text;
  el.className = cls;
}

function renderRec(s) {
  recording = !!s.recording;
  const btn = $('recbtn');
  btn.classList.toggle('on', recording);
  btn.textContent = recording ? '■ STOP REC' : '● REC';
  const banner = $('recbanner');
  banner.classList.toggle('hidden', !recording);
  $('rectime').textContent = (s.recording_s || 0).toFixed(1) + 's';
  const max = s.record_max_s || 0;
  banner.classList.toggle('warn', recording && max > 0 && s.recording_s > max * 0.85);
  $('recmax').textContent = max > 0
    ? (recording && s.recording_s > max * 0.85
        ? 'close to the ~' + Math.round(max) + 's buffer limit — cut now'
        : 'buffer keeps ~' + Math.round(max) + 's')
    : '';
}

function renderLog(lines) {
  const el = $('log');
  const text = lines && lines.length ? lines.slice().reverse().join('\\n') : 'no activity yet';
  if (el.dataset.sig === text) return;
  el.dataset.sig = text;
  el.textContent = text;
  el.scrollTop = 0;
}

async function tick() {
  try {
    const s = await (await fetch('/api/state')).json();
    threshold = s.threshold || 0.62;
    outDir = s.out_dir || '';
    $('score').innerHTML = s.score.toFixed(2) + '<small> / 1.00</small>';
    $('scorecard').classList.toggle('capturing', !!s.capturing || !!s.recording);
    if (!s.connected) setState('offline', 'off');
    else if (s.recording) setState('recording', 'rec');
    else if (s.capturing) setState('clipping', 'rec');
    else if (s.cooldown > 0) setState('cooldown', 'cool');
    else setState('armed', 'armed');
    $('thr').textContent = 'trigger ' + threshold.toFixed(2);
    $('thr2').textContent = threshold.toFixed(2);
    $('bchat').style.width = pct(s.signals.chat || 0);
    $('btext').style.width = pct(s.signals.text || 0);
    $('baudio').style.width = pct(s.signals.audio || 0);
    $('vchat').textContent = (s.signals.chat_users || 0) + 'u/' + (s.signals.consensus || 0) + 'c';
    $('vaudio').textContent = s.signals.audio_db == null ? '—' : s.signals.audio_db.toFixed(1) + 'dB';
    $('vtext').textContent = s.signals.keywords || 0;
    const need = s.signals.required || 1, have = s.signals.agreeing || 0;
    $('bgate').style.width = pct(Math.min(1, have / need));
    $('bgate').parentElement.classList.toggle('hot', need > 1 && have < need);
    $('vgate').textContent = have + '/' + need;
    $('chan').textContent = s.channel || '—';
    document.body.classList.toggle('console', !!s.console);
    $('uptime').textContent = s.uptime_h || '0h 00m';
    $('clips').textContent = s.clips.length;
    $('count').textContent = s.clips.length
      ? s.clips.length + ' clip' + (s.clips.length === 1 ? '' : 's') : 'nothing yet';
    $('buffer').textContent = (s.buffer_h || '—').split(' / ')[0];
    $('cd').textContent = s.recording ? 'recording'
      : (s.cooldown > 0 ? Math.ceil(s.cooldown) + 's cooldown' : 'ready');
    $('m-layout').textContent = 'layout ' + (s.layout || '—');
    $('m-cam').textContent = 'cam ' + (s.cam || '—');
    $('m-style').textContent = s.capture_only ? 'capture only'
      : 'subs ' + (s.caption_style || '—');
    $('m-enc').textContent = 'enc ' + (s.encoder || '—');
    $('dot').style.background = s.connected || s.console ? 'var(--cool)' : 'var(--hot)';
    $('conn').textContent = s.console ? 'console' : (s.connected ? 'live' : 'offline');
    setAuto(!!s.auto);
    renderRec(s);
    renderStreams(s);
    renderVod(s.vod);
    renderStyleJob(s.style);
    renderStyles(s.styles);
    renderLog(s.log);
    history.push(s.score || 0);
    if (history.length > MAXH) history.shift();
    drawSpark();
    if (s.clips.length !== lastCount) { lastCount = s.clips.length; renderWall(s.clips); }
  } catch (e) {
    $('dot').style.background = 'var(--hot)';
    $('conn').textContent = 'offline';
    setState('disconnected', 'off');
  }
}

function renderWall(clips) {
  const wall = $('wall');
  if (!clips.length) {
    wall.innerHTML = document.body.classList.contains('console')
      ? '<div class="empty">clips land in each streamer\\'s folder — open their console to watch</div>'
      : '<div class="empty">waiting for the first moment...</div>';
    return;
  }
  wall.innerHTML = clips.map(c => `
    <div class="clip">
      <video src="/clips/${encodeURIComponent(c.file)}" controls preload="metadata" playsinline></video>
      <div class="meta">
        <span class="title" title="${esc(c.title)}">${esc(c.title || c.file)}</span>
        <span class="pill">${c.uploaded || c.capture_only ? 'raw' : esc(c.score)}</span>
      </div>
      <label class="pick">
        ${c.styled ? '<span class="chip">' + esc(c.style || 'styled') + '</span>'
                   : '<input type="checkbox" class="clipick" value="' + esc(c.file) + '"> style this'}
      </label>
      <div class="actions">
        <a class="iconbtn" href="/clips/${encodeURIComponent(c.file)}?download=1" download>download</a>
        <button class="iconbtn" onclick="copyPath('${esc(c.file)}')">copy path</button>
        <button class="iconbtn danger" onclick="delClip('${esc(c.file)}')">delete</button>
      </div>
    </div>`).join('');
}

document.addEventListener('keydown', e => {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.target && e.target.matches('input, select, textarea')) return;
  const k = e.key.toLowerCase();
  if (k === 'escape' && !$('choicemodal').classList.contains('hidden')) {
    e.preventDefault();
    resolveChoice('cancel');
  }
  else if (k === 'r') {
    if (document.body.classList.contains('console')) return;
    e.preventDefault(); toggleRec();
  }
  else if (k === 'a') { e.preventDefault(); toggleAuto(); }
  else if (k === 'c') {
    if (document.body.classList.contains('console')) return;
    e.preventDefault(); snap();
  }
  else if (k === 's') { e.preventDefault(); profileAction('save'); }
});

async function init() {
  try {
    const data = await (await fetch('/api/settings')).json();
    buildSettings(data.spec, data.groups);
    fillSettings(data, true);
  } catch (e) {
    $('settings').innerHTML = '<div class="empty">could not load settings</div>';
  }
  renderWall([]);
  drawSpark();
  tick();
  setInterval(tick, 1000);
}
init();
</script>
</body>
</html>
"""


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


class _Handler(BaseHTTPRequestHandler):
    engine = None
    clips_dir = Path("clips")
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: A003 - silence per-request spam
        return

    # -- routing -------------------------------------------------------
    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            return self._text(PAGE.encode("utf-8"), "text/html; charset=utf-8")
        if path == "/api/state":
            return self._json(self.engine.snapshot())
        if path == "/api/settings":
            return self._json(self.engine.settings_snapshot())
        if path == "/api/streams":
            return self._json({"ok": True, "streams": self.engine.streams.snapshot()})
        if path.startswith("/clips/"):
            download = "download=1" in (parsed.query or "")
            return self._media(unquote(path[len("/clips/"):]), download=download)
        if path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        self.send_error(404)
        return None

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if not self._same_origin():
            # The body is still on the socket; close instead of letting it be
            # parsed as the next keep-alive request.
            self.close_connection = True
            return self._json({"ok": False, "error": "cross-origin request refused"},
                              status=403)
        # An upload is raw file bytes, not JSON: stream it before the small
        # JSON reader gets a chance to swallow the first megabyte.
        if path == "/api/upload":
            return self._upload(parsed.query)
        body = self._read_json()
        engine = self.engine
        try:
            if path == "/api/clip":
                action = str(body.get("action") or "snap")
                if action == "start":
                    return self._json(engine.start_recording())
                if action == "stop":
                    return self._json(engine.stop_recording())
                if action == "toggle":
                    return self._json(engine.toggle_recording())
                engine.force_clip()
                return self._json({"ok": True, "note": "clip requested"})
            if path == "/api/settings":
                values = body.get("values")
                return self._json(engine.apply_settings(values if isinstance(values, dict) else {}))
            if path == "/api/auto":
                on = _truthy(body["on"]) if "on" in body else not engine.settings_snapshot()["auto"]
                return self._json(engine.apply_auto(on))
            if path == "/api/autoconfigure":
                return self._json(engine.auto_configure())
            if path == "/api/profile":
                action = str(body.get("action") or "save")
                if action == "load":
                    return self._json(engine.load_profile())
                if action == "reset":
                    return self._json(engine.reset_profile())
                return self._json(engine.save_profile())
            if path == "/api/camera":
                return self._json(engine.redetect_cam())
            if path == "/api/clips/delete":
                return self._json(engine.delete_clip(str(body.get("file") or "")))
            if path == "/api/streams":
                return self._json(engine.add_stream(
                    str(body.get("url") or body.get("link") or ""),
                    str(body.get("name") or ""),
                    str(body.get("mode") or "ask"),
                ))
            if path == "/api/streams/stop":
                return self._json(engine.stop_stream(str(body.get("name") or "")))
            if path == "/api/streams/retry":
                return self._json(engine.retry_stream(str(body.get("name") or "")))
            if path == "/api/vod":
                try:
                    max_clips = int(body.get("max_clips") or 12)
                except (TypeError, ValueError):
                    max_clips = 12
                return self._json(engine.start_vod(
                    str(body.get("url") or body.get("link") or ""),
                    str(body.get("name") or ""),
                    max(1, min(max_clips, 60)),
                    str(body.get("chat_mode") or "auto"),
                    bool(body.get("reset")),
                ))
            if path == "/api/vod/stop":
                return self._json(engine.stop_vod())
            if path == "/api/style":
                files = body.get("files")
                if not isinstance(files, list):
                    files = []
                return self._json(engine.start_style(
                    [str(name) for name in files],
                    str(body.get("style") or "clean"),
                ))
            if path == "/api/style/stop":
                return self._json(engine.stop_style())
            if path == "/api/update":
                action = str(body.get("action") or "check")
                return self._json(engine.run_update() if action == "apply"
                                  else engine.check_update())
        except Exception as exc:  # noqa: BLE001 - a bad request must not kill the console
            return self._json({"ok": False, "error": str(exc)}, status=500)
        self.send_error(404)
        return None

    # -- helpers -------------------------------------------------------
    def _same_origin(self) -> bool:
        """Refuse a browser request from another site.

        The console is loopback-only, but any page the operator has open can
        still POST to 127.0.0.1. Requests without an ``Origin`` header (curl,
        the tests, a native client) are allowed; a browser request must match
        the host that served this page.
        """
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return urlparse(origin).netloc == (self.headers.get("Host") or "")

    def _upload(self, query: str):
        """Stream a dropped raw clip onto the clip wall.

        The browser sends the file as the request body (not multipart), so the
        bytes go straight to disk in 1 MiB chunks without buffering a
        multi-gigabyte recording in memory. The clip is normalized to a
        browser-playable MP4, moved into ``--out`` with a sidecar, and appears
        on the wall where it can be ticked and styled.
        """
        engine = self.engine
        params = parse_qs(query or "")
        raw_name = (params.get("name") or [""])[0]
        try:
            stem, suffix = upload_name(raw_name)
        except ValueError as exc:
            self.close_connection = True
            return self._json({"ok": False, "error": str(exc)}, status=400)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            self.close_connection = True
            return self._json(
                {"ok": False, "error": "the browser sent no file data"},
                status=400)
        if length > UPLOAD_LIMIT:
            self.close_connection = True
            return self._json(
                {"ok": False,
                 "error": f"that file is larger than the {UPLOAD_LIMIT // 1024**3} GB limit"},
                status=413)

        work_dir = Path(getattr(engine.cfg, "work_dir", ".clipper"))
        # Absolute, so the path works in a later rerun even if the console's
        # working directory changes.
        folder = (work_dir / "uploads").resolve()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(folder).free
        except OSError as exc:
            self.close_connection = True
            return self._json({"ok": False, "error": f"cannot use {folder}: {exc}"},
                              status=500)
        # The raw upload and the normalized MP4 briefly coexist.
        if length * 2 + UPLOAD_RESERVE > free:
            self.close_connection = True
            return self._json(
                {"ok": False,
                 "error": f"not enough free disk space for this clip — "
                          f"{free / 1024**3:.1f} GB free"},
                status=507)

        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = folder / f"{stem}-{stamp}{suffix}"
        serial = 2
        while target.exists():
            target = folder / f"{stem}-{stamp}_{serial}{suffix}"
            serial += 1

        received = 0
        try:
            # A client that stalls mid-upload must not pin this handler forever.
            self.connection.settimeout(120)
        except OSError:
            pass
        try:
            with open(target, "wb") as fh:
                while received < length:
                    chunk = self.rfile.read(min(1 << 20, length - received))
                    if not chunk:
                        break
                    fh.write(chunk)
                    received += len(chunk)
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            target.unlink(missing_ok=True)
            return self._json({"ok": False, "error": f"could not save the upload: {exc}"},
                              status=500)
        if received != length:
            target.unlink(missing_ok=True)
            return self._json(
                {"ok": False,
                 "error": f"upload was interrupted after {received} of {length} bytes"},
                status=400)

        info = probe(target)
        duration = float(info.get("duration") or 0.0)
        if duration <= 0 or not info.get("vcodec"):
            target.unlink(missing_ok=True)
            return self._json(
                {"ok": False,
                 "error": "that file is not a readable video — try MP4, MOV or MKV"},
                status=400)

        out_root = Path(getattr(engine.cfg, "out_dir", "clips")).resolve()
        temp_dir = out_root / ".incoming"
        try:
            out_root.mkdir(parents=True, exist_ok=True)
            temp_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            target.unlink(missing_ok=True)
            return self._json(
                {"ok": False, "error": f"cannot use the clips folder: {exc}"},
                status=500)

        # Converting and filing are the heavy part. Two tabs dropping at once
        # must not race for the same free filename, so only one import runs;
        # the loser keeps its uploaded file and can retry.
        if not _IMPORT_LOCK.acquire(blocking=False):
            target.unlink(missing_ok=True)
            return self._json(
                {"ok": False, "error": "another clip is still being added — try again in a moment"},
                status=409)
        temp: Path | None = None
        try:
            final = free_clip_name(out_root, stem)
            temp = temp_dir / f"{final.stem}.mp4"
            problem = normalize_clip(target, temp, info)
            if problem:
                engine.log(f"upload rejected — {stem}: {problem}")
                return self._json(
                    {"ok": False, "error": f"could not convert that clip: {problem}"},
                    status=400)

            normalized = probe(temp)
            duration = float(normalized.get("duration") or duration)
            width = int(normalized.get("width") or info.get("width") or 0)
            height = int(normalized.get("height") or info.get("height") or 0)
            if duration <= 0 or not width or not height:
                return self._json(
                    {"ok": False, "error": "the converted clip has no picture or length"},
                    status=400)

            try:
                os.replace(temp, final)
            except OSError as exc:
                return self._json({"ok": False, "error": f"could not save the clip: {exc}"},
                                  status=500)
        finally:
            _IMPORT_LOCK.release()
            target.unlink(missing_ok=True)
            if temp is not None:
                temp.unlink(missing_ok=True)

        # The sidecar is how the wall and the styler know this is a raw clip
        # rather than a capture from a live run. ``score`` is zero because no
        # trigger chose it; it is here because the operator dropped it.
        title = Path(str(raw_name).replace("\\", "/")).stem.strip() or stem
        # A portrait capture already has the frame the console is configured
        # for (split/duo/blur), and that is what the seam-aware styles need.
        # Landscape footage is left alone and anchored as a horizontal frame.
        layout = "horizontal"
        if height >= width:
            layout = str(getattr(engine.cfg, "layout", "vertical") or "vertical")
            if layout not in ("split", "duo", "vertical", "blur"):
                layout = "vertical"
        sidecar = {
            "title": title[:80],
            "score": 0,
            "source": "upload",
            "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "layout_used": layout,
            "duration": round(duration, 2),
            "width": width,
            "height": height,
            "signals": {},
            "chat_excerpt": [],
        }
        try:
            final.with_suffix(".json").write_text(
                json.dumps(sidecar, indent=2), encoding="utf-8")
        except OSError as exc:
            engine.log(f"upload — could not write the sidecar for {final.name}: {exc}")

        engine.log(f"raw clip added — {final.name} ({duration:.1f}s, "
                   f"{width}x{height}, {received / 1024**2:.0f} MB)")
        return self._json({
            "ok": True,
            "file": final.name,
            "title": sidecar["title"],
            "duration": duration,
            "width": width,
            "height": height,
            "layout": sidecar["layout_used"],
            "size": received,
        })

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0:
            return {}
        try:
            raw = self.rfile.read(min(length, 1 << 20))
        except OSError:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _text(self, body: bytes, ctype: str, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status: int = 200):
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _media(self, name: str, download: bool = False):
        target = (self.clips_dir / Path(name).name).resolve()
        if not target.exists() or target.suffix != ".mp4":
            self.send_error(404)
            return
        size = target.stat().st_size
        start, end = 0, size - 1
        header = self.headers.get("Range", "")
        partial = False
        if header.startswith("bytes="):
            spec = header[6:].split(",")[0]
            a, _, b = spec.partition("-")
            try:
                if a:
                    start = int(a)
                if b:
                    end = min(int(b), size - 1)
                partial = True
            except ValueError:
                start, end, partial = 0, size - 1, False
            if start >= size or start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if download:
            self.send_header("Content-Disposition",
                             f'attachment; filename="{target.name}"')
        self.end_headers()
        with open(target, "rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(262144, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                remaining -= len(chunk)


class Dashboard(threading.Thread):
    def __init__(self, engine, port: int, clips_dir: Path):
        super().__init__(daemon=True, name="dashboard")
        self.engine = engine
        self.port = port
        self.clips_dir = clips_dir
        self.httpd: ThreadingHTTPServer | None = None

    def run(self):
        handler = type("BoundHandler", (_Handler,), {
            "engine": self.engine,
            "clips_dir": self.clips_dir,
        })
        wanted = self.port
        for port in self._ports_to_try():
            try:
                self.httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
            except OSError:
                self.httpd = None
                continue
            self.port = port
            break
        if self.httpd is None:
            self.engine.log(f"dashboard unavailable on port {wanted}: no free port nearby")
            return
        if self.port != wanted:
            self.engine.log(f"port {wanted} was busy — dashboard moved to {self.port}")
        self.httpd.daemon_threads = True
        url = f"http://127.0.0.1:{self.port}"
        self.engine.log(f"dashboard → {url}")
        if getattr(self.engine.cfg, "open_browser", False):
            try:
                import webbrowser

                webbrowser.open(url + "/")
            except Exception:  # noqa: BLE001 - a missing browser must not stop the console
                pass
        self.httpd.serve_forever(poll_interval=0.5)

    def _ports_to_try(self) -> list[int]:
        """The requested port, then nearby ones when it is the shared default.

        Two clipper runs on one machine would otherwise fight over 8756 and the
        loser would silently have no console. An explicitly chosen port is
        respected exactly; only the default walks forward to the next free one.
        """
        if self.port != 8756:
            return [self.port]
        return list(range(8756, 8766))

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
