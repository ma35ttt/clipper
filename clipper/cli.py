"""Command line interface."""

from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path

from . import __version__
from .chat import parse_target
from .config import DEFAULT_CAM, LAYOUTS, PRESETS, SUBS_MODES, SUBS_STYLES, TITLE_MODES, VERIFY_MODES, Config, apply_preset, parse_cam
from .util import FFMPEG, FFPROBE, find_font, fmt_bytes, fmt_clock, have, run

BANNER = r"""
      _ _                       
  ___| (_)_ __  _ __   ___ _ _  
 / __| | | '_ \| '_ \ / _ \ '__|
| (__| | | |_) | |_) |  __/ |   
 \___|_|_| .__/| .__/ \___|_|   
         |_|   |_|   realtime livestream clipper
"""


def _add_run_args(p: argparse.ArgumentParser, include_source: bool = True) -> None:
    if include_source:
        p.add_argument("-i", "--input", help="HLS/RTMP URL, or a local file/device")
        p.add_argument("-c", "--chat", help="twitch:<channel> | file:<chat.jsonl> | sim")
        p.add_argument("--name",
                       help="display/profile name (defaults to the chat channel)")
    p.add_argument("--out", default="clips", help="where clips are written (default: clips)")
    p.add_argument("--work", default=".clipper", help="scratch directory (default: .clipper)")
    p.add_argument("--profile", choices=sorted(PRESETS), help="trigger tuning preset")
    p.add_argument("--layout", choices=LAYOUTS, help="output framing (default: split)")
    p.add_argument("--cam",
                   help="webcam box x:y:w:h in source pixels for --layout split, or "
                        "'auto' to detect the streamer's face and follow them "
                        "(default: auto; if no face is found, split uses the "
                        "blurred frame instead of guessing a crop)")
    p.add_argument("--cam-interval", type=float,
                   help="seconds between automatic camera re-detections (default: 90)")
    p.add_argument("--threshold", type=float, help="hype trigger threshold 0..1 (default: 0.62)")
    p.add_argument("--confirm", type=float, help="seconds above threshold before triggering")
    p.add_argument("--cooldown", type=float, help="minimum seconds between clips")
    p.add_argument("--pre", type=float, help="seconds of lead-in before the trigger")
    p.add_argument("--post", type=float, help="seconds of tail after the trigger")
    p.add_argument("--max-post", type=float, help="cap on auto-extension while hype continues")
    p.add_argument("--agree", type=int,
                   help="independent signals that must agree before triggering "
                        "(default: 2 when chat and audio are both live, else 1; "
                        "set 1 to let a single signal fire)")
    p.add_argument("--chat-lag", type=float, help="viewer delay vs ingest, in seconds (default: 3)")
    p.add_argument("--title-mode", choices=TITLE_MODES, help="hook caption on the clip")
    p.add_argument("--title", help="fixed caption text when --title-mode fixed")
    p.add_argument("--subs", choices=SUBS_MODES, help="burn in speech subtitles (default: auto)")
    p.add_argument("--subs-style", choices=SUBS_STYLES,
                   help="subtitle look: 'pop' word-chunks on a pill (default), "
                        "or 'block' text lines")
    p.add_argument("--subs-model", help="path to a whisper ggml model (default: auto)")
    p.add_argument("--subs-lang", help="spoken language for subtitles (default: en)")
    p.add_argument("--subs-prompt",
                   help="names/game terms to bias subtitles toward, e.g. \"Jynxzi, R6 Siege\"")
    p.add_argument("--subs-threads", type=int,
                   help="whisper CPU threads (default: half the cores, max 4; "
                        "0 auto, negative = all cores)")
    p.add_argument("--verify", choices=VERIFY_MODES,
                   help="verify flagged candidates with a fast transcript "
                        "(default: auto — on when a whisper model is present)")
    p.add_argument("--verify-interval", type=float,
                   help="minimum seconds between verifications (default: 15)")
    p.add_argument("--encoder", help="video encoder: auto | libx264 | h264_videotoolbox")
    p.add_argument("--encoder-preset", help="x264 preset (default: veryfast)")
    p.add_argument("--crf", type=int, help="x264 quality, lower = better (default: 20)")
    p.add_argument("--segment-time", type=float, help="buffer segment length (default: 2s)")
    p.add_argument("--loop", action="store_true", help="loop a local file (demo/testing)")
    p.add_argument("--transcode", action="store_true", help="re-encode on ingest")
    p.add_argument("--speed", type=float, help="read-rate multiplier for local files")
    p.add_argument("--port", type=int, help="dashboard port (default: 8756)")
    p.add_argument("--no-dashboard", action="store_true", help="disable the local console")
    p.add_argument("--no-profile", action="store_true",
                   help="do not save or load per-streamer tuning")
    p.add_argument("--run-for", type=float, help="stop after N seconds")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--json", action="store_true", help="print the effective config and exit")


def _config_from_args(args: argparse.Namespace) -> Config:
    cfg = Config()
    if args.profile:
        apply_preset(cfg, args.profile)
    mapping = {
        "input": getattr(args, "input", None),
        "name": getattr(args, "name", None),
        "chat": getattr(args, "chat", None),
        "threshold": args.threshold,
        "confirm": args.confirm,
        "cooldown": args.cooldown,
        "pre": args.pre,
        "post": args.post,
        "max_post": args.max_post,
        "agree": args.agree,
        "chat_lag": args.chat_lag,
        "layout": args.layout,
        "cam": args.cam,
        "cam_interval": args.cam_interval,
        "title_mode": args.title_mode,
        "title": args.title,
        "subs": args.subs,
        "subs_style": args.subs_style,
        "subs_model": args.subs_model,
        "subs_language": args.subs_lang,
        "subs_prompt": args.subs_prompt,
        "subs_threads": args.subs_threads,
        "verify": args.verify,
        "verify_interval": args.verify_interval,
        "encoder": args.encoder,
        "preset": args.encoder_preset,
        "crf": args.crf,
        "segment_time": args.segment_time,
        "speed": args.speed,
        "loop": args.loop or None,
        "transcode": args.transcode or None,
        "port": args.port,
        "run_for": args.run_for,
        "verbose": args.verbose or None,
        "profiles": False if args.no_profile else None,
        "console": getattr(args, "console", False) or None,
        "open_browser": getattr(args, "open_browser", False) or None,
    }
    for key, value in mapping.items():
        if value is not None:
            setattr(cfg, key, value)
    if args.out:
        cfg.out_dir = Path(args.out)
    if args.work:
        cfg.work_dir = Path(args.work)
    if not args.no_dashboard:
        cfg.dashboard = True
    cfg.validate()
    return cfg


def cmd_run(args: argparse.Namespace) -> int:
    from .engine import Engine

    cfg = _config_from_args(args)
    if args.json:
        print(json.dumps(cfg.as_dict(), indent=2))
        return 0
    print(BANNER)
    if not cfg.chat:
        print("  no chat source — audio-only triggers. Add --chat twitch:<channel>\n")
    engine = Engine(cfg)
    try:
        return engine.run()
    except KeyboardInterrupt:
        engine.stop()
        return 130
    except SystemExit:
        raise


def cmd_dashboard(args: argparse.Namespace) -> int:
    """Console-only mode: no stream until a link is pasted into the page."""
    from .engine import Engine

    cfg = _config_from_args(args)
    cfg.console = True
    cfg.dashboard = True
    if args.json:
        print(json.dumps(cfg.as_dict(), indent=2))
        return 0
    print(BANNER)
    engine = Engine(cfg)
    try:
        return engine.run()
    except KeyboardInterrupt:
        engine.stop()
        return 130
    except SystemExit:
        raise


def cmd_doctor(args: argparse.Namespace) -> int:
    ok = True
    rows: list[tuple[str, str, bool]] = []

    rows.append(("clipper", __version__, True))
    for name, binary in (("ffmpeg", FFMPEG), ("ffprobe", FFPROBE)):
        if not have(binary):
            rows.append((name, "NOT FOUND", False))
            ok = False
            continue
        p = run([binary, "-version"], timeout=15)
        version = (p.stdout or "").splitlines()[0] if p.stdout else "unknown"
        rows.append((name, version[:64], p.returncode == 0))

    if have(FFMPEG):
        enc = run([FFMPEG, "-hide_banner", "-encoders"], timeout=20).stdout or ""
        for codec in ("libx264", "aac"):
            present = codec in enc
            rows.append((f"encoder:{codec}", "available" if present else "MISSING", present))
            ok = ok and present

    font = find_font()
    rows.append(("caption font", font or "not found (captions disabled)", bool(font)))

    from . import caption as caption_mod

    styled = caption_mod.has_styled()
    style = (f"ImageMagick · {Path(caption_mod.styled_font()).name}" if styled
             else "bitmap fallback (brew install imagemagick for outlined text)")
    rows.append(("caption style", style, styled))

    from . import camera as camera_mod

    if camera_mod.available():
        rows.append(("camera detect", "Vision face detection (macOS)", True))
    else:
        rows.append(("camera detect", "manual --cam only (needs macOS + Swift)", False))

    from . import transcribe

    ready, detail = transcribe.status()
    rows.append(("subtitles", detail, ready))

    target = Path(args.out)
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe_file = target / ".write-test"
        probe_file.write_text("x")
        probe_file.unlink()
        rows.append(("output dir", f"{target.resolve()} writable", True))
    except OSError as exc:
        rows.append(("output dir", f"not writable: {exc}", False))
        ok = False

    if args.chat:
        kind, value = parse_target(args.chat)
        if kind == "twitch":
            try:
                with socket.create_connection(("irc.chat.twitch.tv", 6697), timeout=6):
                    pass
                rows.append(("twitch irc", "reachable (TLS 6697)", True))
            except OSError as exc:
                rows.append(("twitch irc", f"unreachable: {exc}", False))
                ok = False
        elif kind == "file":
            exists = Path(value).exists()
            rows.append(("chat file", str(value), exists))
            ok = ok and exists

    width = max(len(r[0]) for r in rows)
    for label, value, good in rows:
        mark = "✓" if good else "✗"
        print(f"  {mark} {label.ljust(width)}  {value}")
    print()
    print("ready to clip." if ok else "fix the ✗ items above before streaming.")
    return 0 if ok else 1


def _forward_args(args: argparse.Namespace) -> list[str]:
    """Translate shared options into `clipper run` flags for worker processes."""
    pairs = [
        ("--layout", args.layout), ("--cam", args.cam), ("--profile", args.profile),
        ("--cam-interval", args.cam_interval),
        ("--threshold", args.threshold), ("--confirm", args.confirm),
        ("--cooldown", args.cooldown), ("--pre", args.pre), ("--post", args.post),
        ("--max-post", args.max_post), ("--chat-lag", args.chat_lag),
        ("--agree", args.agree),
        ("--title-mode", args.title_mode), ("--title", args.title),
        ("--subs", args.subs), ("--subs-style", args.subs_style),
        ("--subs-model", args.subs_model),
        ("--subs-lang", args.subs_lang),
        ("--subs-prompt", args.subs_prompt),
        ("--subs-threads", args.subs_threads),
        ("--verify", args.verify), ("--verify-interval", args.verify_interval),
        ("--encoder", args.encoder), ("--encoder-preset", args.encoder_preset),
        ("--crf", args.crf), ("--segment-time", args.segment_time),
        ("--speed", args.speed),
    ]
    out: list[str] = []
    for flag, value in pairs:
        if value is not None:
            out += [flag, str(value)]
    name = getattr(args, "name", None)
    if name:
        out += ["--name", str(name)]
    for flag, value in (("--loop", args.loop), ("--transcode", args.transcode)):
        if value:
            out.append(flag)
    if getattr(args, "no_profile", False):
        out.append("--no-profile")
    return out


def cmd_models(args: argparse.Namespace) -> int:
    from . import transcribe

    if args.get:
        try:
            path = transcribe.download(args.get, Path(args.dir))
        except Exception as exc:  # noqa: BLE001 - network is unreliable by nature
            print(f"error: could not download {args.get}: {exc}")
            return 1
        print(f"  model → {path}")
        ok, detail = transcribe.status(str(path))
        print(f"  {'ready' if ok else 'note'}: {detail}")
        return 0

    ok, detail = transcribe.status(args.model)
    print(f"  subtitles: {'ready' if ok else 'not ready'} — {detail}")
    print(f"  binary:    {transcribe.whisper_binary() or 'not installed'}")
    model = transcribe.find_model(args.model)
    print(f"  model:     {model or 'none found'}")
    vad = transcribe.find_vad_model()
    print(f"  vad:       {vad or 'not found (optional, big accuracy win — clipper models --get vad)'}")
    print(f"  searched:  {', '.join(str(d) for d in transcribe.MODEL_DIRS)}")
    if model and "tiny" in model.name:
        print("\n  accuracy tip: base.en / small.en / medium.en are noticeably better on names and slang")
        print("                clipper models --get medium.en")
    print("\n  fetch a model:  clipper models --get medium.en")
    print("  fetch the VAD:  clipper models --get vad")
    return 0


def cmd_multi(args: argparse.Namespace) -> int:
    from .multi import Multi, free_port, parse_channels

    specs = list(args.channel or [])
    if args.channels:
        specs.append(args.channels)
    out_root = Path(args.out)
    work_root = Path(args.work)
    port_base = args.port or 8756
    channels = parse_channels(specs, out_root, work_root, port_base)
    base = free_port(port_base, len(channels) + 1)
    for index, channel in enumerate(channels):
        channel.port = base + 1 + index

    print(BANNER)
    print(f"  {len(channels)} channel(s): {', '.join(c.name for c in channels)}")
    print(f"  console: http://127.0.0.1:{base}   clips: {out_root}/\n")
    runner = Multi(channels, base, _forward_args(args), run_for=args.run_for or 0.0)
    return runner.run()


def cmd_clips(args: argparse.Namespace) -> int:
    directory = Path(args.out)
    if not directory.exists():
        print(f"no clips yet ({directory} does not exist)")
        return 0
    files = sorted(directory.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        print(f"no clips yet in {directory}")
        return 0
    print(f"{len(files)} clip(s) in {directory}\n")
    for path in files[: args.limit]:
        meta = {}
        side = path.with_suffix(".json")
        if side.exists():
            try:
                meta = json.loads(side.read_text())
            except ValueError:
                meta = {}
        size = fmt_bytes(path.stat().st_size)
        duration = meta.get("duration_s") or 0
        score = meta.get("score")
        score_txt = f"score {score:.2f}" if isinstance(score, (int, float)) else "score  ?  "
        title = (meta.get("title") or "").strip()
        print(f"  {path.name:<46} {fmt_clock(duration):>6}  {size:>8}  {score_txt}  {title}")
    return 0


def cmd_frame(args: argparse.Namespace) -> int:
    """Grab one frame and draw the webcam box on it, so the split layout lines up.

    With the default ``--cam auto`` the box is the one clipper detected from the
    stream, so the picture shows exactly what the split layout will crop.
    """
    from . import camera
    from . import caption
    from .resolve import resolve
    from .util import FFMPEG, run

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    url = args.input
    if url.lower().startswith("http") and "twitch.tv" in url:
        try:
            url = resolve(args.input).url
        except RuntimeError as exc:
            print(f"error: {exc}")
            return 1

    spec = args.cam
    if spec == "auto":
        box = camera.detect(url, Path(args.work), frames=8, log=lambda m: print(f"  {m}"))
        if box is None:
            if not camera.available():
                print("  camera auto-detect unavailable (needs macOS + Swift) — "
                      f"using fallback {DEFAULT_CAM}")
            else:
                print(f"  no face found — using fallback {DEFAULT_CAM}")
            spec = DEFAULT_CAM
        else:
            spec = camera.format_box(box)

    grabbed = run(
        [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
         "-i", url, "-frames:v", "1", str(out)],
        timeout=120,
    )
    if grabbed.returncode != 0 or not out.exists():
        print("error: could not grab a frame from that source")
        return 1

    try:
        x, y, w, h = parse_cam(spec)
    except ValueError as exc:
        print(f"error: --cam {exc}")
        return 1

    magick = caption.magick_binary()
    drawn = False
    if magick:
        res = run([magick, str(out), "-stroke", "red", "-strokewidth", "5",
                   "-fill", "none", "-draw", f"rectangle {x},{y} {x + w},{y + h}",
                   str(out)], timeout=60)
        drawn = res.returncode == 0

    print(f"  frame → {out.resolve()}")
    print(f"  cam   → {x}:{y}:{w}:{h}" +
          ("" if drawn else "   (install imagemagick to see the box drawn)"))
    if args.cam == "auto":
        print(f"  pass it on with:  --cam {x}:{y}:{w}:{h}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="clipper",
        description="Cut clips out of a live stream in realtime.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  clipper run -i https://twitch.tv/xqc -c twitch:xqc --layout blur\n"
            "  clipper run -i stream.m3u8 -c twitch:kaicenat --profile sensitive\n"
            "  clipper run -i demo.mp4 --loop -c sim --no-dashboard\n"
            "  clipper multi --channels xqc,kaicenat,caseoh\n"
        ),
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="start clipping")
    _add_run_args(p_run)
    p_run.set_defaults(func=cmd_run)

    p_multi = sub.add_parser("multi", help="clip several streamers at once")
    _add_run_args(p_multi, include_source=False)
    p_multi.add_argument("--channels", help="comma list: xqc,kaicenat,caseoh")
    p_multi.add_argument("--channel", action="append", metavar="NAME|INPUT|CHAT",
                         help="one channel, repeatable: 'kai|twitch.tv/kaicenat|twitch:kaicenat'")
    p_multi.set_defaults(func=cmd_multi)

    p_dash = sub.add_parser("dashboard", aliases=["console"],
                            help="open the console only; add stream links in the page")
    _add_run_args(p_dash, include_source=False)
    p_dash.add_argument("--open", action="store_true", dest="open_browser",
                        help="open the console in your browser once it is listening")
    p_dash.set_defaults(func=cmd_dashboard, console=True)

    p_frame = sub.add_parser("frame", help="grab a frame so you can line up --cam")
    p_frame.add_argument("-i", "--input", required=True, help="stream URL or file")
    p_frame.add_argument("--cam", default="auto",
                         help="webcam box x:y:w:h to draw, or 'auto' to detect "
                              f"(default: auto; fallback {DEFAULT_CAM})")
    p_frame.add_argument("--work", default=".clipper", help="scratch directory")
    p_frame.add_argument("-o", "--out", default="frame.jpg",
                         help="where to write the frame (default: frame.jpg)")
    p_frame.set_defaults(func=cmd_frame)

    p_doc = sub.add_parser("doctor", help="check the environment")
    p_doc.add_argument("--chat", help="also test this chat source")
    p_doc.add_argument("--out", default="clips")
    p_doc.set_defaults(func=cmd_doctor)

    p_clips = sub.add_parser("clips", help="list captured clips")
    p_clips.add_argument("--out", default="clips")
    p_clips.add_argument("--limit", type=int, default=25)
    p_clips.set_defaults(func=cmd_clips)

    p_models = sub.add_parser("models", help="check or fetch subtitle models")
    p_models.add_argument("--get", metavar="NAME",
                          help="download a whisper model: tiny.en, base.en, small.en")
    p_models.add_argument("--dir", default="models", help="where to store models")
    p_models.add_argument("--model", help="check this model path instead")
    p_models.set_defaults(func=cmd_models)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
