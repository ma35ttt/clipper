"""Cut a clip out of the rolling buffer and render it for posting."""

from __future__ import annotations

import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Config, DEFAULT_CAM, parse_cam
from .hls import Segment
from .util import FFMPEG, find_font, pick_encoder, probe, run, safe_name
from . import caption, transcribe

# Phrases that actually signal a moment. The hook is pulled from the clip's own
# transcript, so it says something about *this* clip. A generic "NO WAY" fires on
# every streamer alive; naming what he said is what makes a viewer stop.
HOOK_PHRASES = (
    "oh my god", "what the", "what is he doing", "are you serious",
    "you're trolling", "you are trolling", "im done", "i'm done", "i am done",
    "lets go", "let's go", "no shot", "you're kidding", "shut up", "bro what",
    "no way", "no no no", "wait what", "what happened", "get out of here",
    "holy", "insane", "what is happening", "he's so bad", "he is so bad",
    "i can't", "i cant", "look at this", "watch this",
)


@dataclass
class Prepared:
    work: Path
    list_path: Path
    offset: float
    duration: float
    width: int
    height: int
    title_file: Path | None


def hook_from_cues(cues) -> str:
    """Pull the most quotable short phrase out of the clip's own transcript.

    Returns "" when nothing quotable was said — an honest empty hook beats a
    caption that says nothing about the clip.
    """
    text = " ".join(c.text for c in cues).lower()
    text = re.sub(r"[^a-z0-9' ]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    best = ""
    for phrase in HOOK_PHRASES:
        if phrase in text and len(phrase) > len(best):
            best = phrase
    return best.upper() if best else ""


def pick_title(cfg: Config, signals: dict, cues=()) -> str:
    if cfg.title_mode == "none":
        return ""
    if cfg.title_mode == "fixed":
        return cfg.title or ""
    return hook_from_cues(cues)


def _wrap(text: str, width: int = 20) -> str:
    words, lines, current = text.split(), [], ""
    for word in words:
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return "\n".join(lines[:3])


# -- `--layout split` geometry, on a 1080x1920 frame -------------------------
SPLIT_CAM_H = 760     # camera band across the top
SPLIT_GAME_H = 608    # 16:9 gameplay at 1080 wide
SPLIT_BOT_H = 1920 - SPLIT_CAM_H - SPLIT_GAME_H   # 552, where captions live

# Candidate verification runs while the loop is live, so it gets fewer threads
# than subtitle transcription — it just needs to be quick, not perfect.
VERIFY_THREADS = 2


def split_graph(cam: str = DEFAULT_CAM, src_w: int = 1920,
                src_h: int = 1080) -> list[str]:
    """Filter parts for cam-on-top / gameplay-middle / captions-below.

    The webcam inset is cropped out of the source and shown across the top over
    a blurred fill of itself, the 16:9 frame sits underneath, and the band left
    over carries the burned subtitles and the hook caption. The small copy of
    the webcam that is baked into the gameplay is blurred away so his face does
    not appear twice.
    """
    x, y, w, h = parse_cam(cam)
    crop = f"crop={w}:{h}:{x}:{y}"

    # Where the inset lands once the source is scaled into the gameplay band.
    if src_w and src_h:
        scale = min(1080 / src_w, SPLIT_GAME_H / src_h)
    else:
        scale = 0.5625
    pad = 6

    def even(v: int) -> int:
        return max(0, int(v) - int(v) % 2)

    sx, sy = even(max(0, x - pad)), even(max(0, y - pad))
    sw, sh = even(w + 2 * pad), even(h + 2 * pad)
    ox, oy = even(sx * scale), even(sy * scale)
    ow, oh = max(2, even(sw * scale)), max(2, even(sh * scale))

    return [
        "[0:v]split=5[c1][c2][c3][c4][c5]",
        f"[c1]{crop},scale=1080:{SPLIT_CAM_H}:force_original_aspect_ratio=increase,"
        f"crop=1080:{SPLIT_CAM_H},gblur=sigma=18,eq=brightness=-0.2[cambg]",
        f"[c2]{crop},scale=-2:{SPLIT_CAM_H}[camfg]",
        "[cambg][camfg]overlay=(W-w)/2:(H-h)/2,format=yuv420p,setsar=1[top]",
        f"[c3]scale=1080:{SPLIT_GAME_H}:force_original_aspect_ratio=decrease[game0]",
        # Blur the duplicate webcam inset that is baked into the gameplay frame.
        f"[c5]crop={sw}:{sh}:{sx}:{sy},scale={ow}:{oh},gblur=sigma=8[patchblur]",
        f"[game0][patchblur]overlay={ox}:{oy},format=yuv420p,setsar=1[game]",
        "[c4]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
        f"gblur=sigma=8,scale=1080:{SPLIT_BOT_H},eq=brightness=-0.3,"
        "format=yuv420p,setsar=1[bot]",
        "[top][game]vstack=inputs=2[tg]",
        "[tg][bot]vstack=inputs=2[vbase]",
    ]


class Cutter:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.encoder = pick_encoder(cfg.encoder)
        self.tmp = cfg.work_dir / "tmp"
        self.tmp.mkdir(parents=True, exist_ok=True)

    # -- step 1: snapshot segments off the live buffer (fast, no encoding) --
    def prepare(self, segments: list[Segment], a: float, b: float, job_id: str) -> Prepared | None:
        usable = [s for s in segments if s.path.exists()]
        if not usable:
            return None
        usable.sort(key=lambda s: s.seq)
        start = usable[0].start
        end = usable[-1].end
        begin = max(a, start)
        finish = min(b, end)
        if finish - begin < 1.0:
            return None

        work = self.tmp / job_id
        if work.exists():
            shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)

        lines = []
        for index, seg in enumerate(usable):
            dest = work / f"{index:04d}.ts"
            try:
                shutil.copy2(seg.path, dest)
            except OSError:
                continue
            lines.append(f"file '{dest.resolve().as_posix()}'")
        if not lines:
            return None
        list_path = work / "concat.txt"
        list_path.write_text("\n".join(lines) + "\n")

        info = probe(usable[0].path)
        return Prepared(
            work=work,
            list_path=list_path,
            offset=begin - start,
            duration=finish - begin,
            width=info.get("width") or 0,
            height=info.get("height") or 0,
            title_file=work / "title.txt",
        )

    # -- step 2: render -----------------------------------------------------
    def render(self, prepared: Prepared, out_path: Path, title: str,
               cues: list | None = None) -> tuple[bool, str, str]:
        """Render the clip. Returns ``(ok, error, warning)``.

        Captions are nice to have, a clip is not — but subtitles are the whole
        point here, so degrade in priority order instead of dropping everything
        on the first failure: keep subtitles and drop the hook caption, then
        drop subtitles, then render bare. Each retry is a fresh ffmpeg process,
        which also rides out the transient jetsam kills this 8 GB machine does.
        """
        # Keep the object as-is: it may be a ``Cues`` carrying word timings that
        # the popping captions need, which ``list(...)`` would throw away.
        cues = cues if cues is not None else []
        tmp_out = prepared.work / "render.mp4"

        plan: list[tuple[str, list, str]] = []
        seen: set[tuple[bool, bool]] = set()

        def add(attempt_title: str, attempt_cues: list, note: str) -> None:
            key = (bool(attempt_title), bool(attempt_cues))
            if key not in seen:
                seen.add(key)
                plan.append((attempt_title, attempt_cues, note))

        add(title, cues, "")
        add("", cues, "hook caption dropped")
        add(title, [], "subtitles dropped")
        add("", [], "all captions dropped" if (title or cues) else "")

        warning = ""
        last_error = ""
        for attempt_title, attempt_cues, note in plan:
            timeout = 900 if not warning else 600
            result = run(self._args(prepared, tmp_out, attempt_title, attempt_cues),
                         timeout=timeout)
            if result.returncode == 0:
                warning = note
                break
            last_error = (result.stderr or "").strip()[-600:]
        else:
            return False, last_error, warning

        out_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp_out, out_path)
        shutil.rmtree(prepared.work, ignore_errors=True)
        return True, "", warning

    # -- step 2b: subtitles -------------------------------------------------
    def subtitle_enabled(self) -> bool:
        mode = getattr(self.cfg, "subs", "off")
        if mode == "off" or not mode:
            return False
        if mode == "on":
            return True
        return transcribe.has_subtitles()

    def caption_style(self) -> str:
        style = getattr(self.cfg, "subs_style", "pop")
        return style if style in ("pop", "block") else "pop"

    def cam_spec(self) -> str:
        """The webcam box to crop with: the detected/known one, or the fallback."""
        spec = getattr(self.cfg, "cam", "") or ""
        return DEFAULT_CAM if spec in ("", "auto") else spec

    def speech_wav(self, prepared: Prepared) -> Path | None:
        """Extract a cleaned 16 kHz mono wav over the clip's exact audio window."""
        wav = prepared.work / "speech.wav"
        args = [
            FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(prepared.list_path),
            "-ss", f"{prepared.offset:.3f}", "-t", f"{prepared.duration:.3f}",
            "-vn",
            # Stream audio is loud, clipped and bass-heavy. Clean it up and
            # level it before whisper sees it — this measurably helps accuracy.
            "-af", "highpass=f=80,loudnorm=I=-16:TP=-1.5:LRA=11",
            "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav),
        ]
        result = run(args, timeout=180)
        if result.returncode != 0 or not wav.exists():
            return None
        return wav

    def transcribe(self, prepared: Prepared, model: str | None = None,
                   threads: int | None = None) -> tuple[list, str]:
        """Speech-to-text over the exact audio window the clip will use."""
        wav = self.speech_wav(prepared)
        if wav is None:
            return [], "could not extract clip audio"
        try:
            return transcribe.transcribe(
                wav,
                model=model if model is not None else getattr(self.cfg, "subs_model", None),
                language=getattr(self.cfg, "subs_language", "en") or "en",
                prompt=getattr(self.cfg, "subs_prompt", None),
                # Cue grouping is only a fallback now: popping captions use the
                # token timings directly, and block captions want readable lines.
                max_chars=44,
                threads=threads if threads is not None
                else getattr(self.cfg, "subs_threads", 0),
            )
        finally:
            try:
                wav.unlink()
            except OSError:
                pass

    def verify(self, prepared: Prepared) -> str:
        """A cheap transcript of a candidate window, for the verifier signal.

        Uses the smallest installed model on few threads, because it only has to
        answer "did he actually say something worth clipping". Returns "" when
        verification is not possible.
        """
        fast = transcribe.find_fast_model()
        if fast is None:
            return ""
        wav = self.speech_wav(prepared)
        if wav is None:
            return ""
        try:
            cues, _ = transcribe.transcribe(wav, model=str(fast), threads=VERIFY_THREADS)
        finally:
            try:
                wav.unlink()
            except OSError:
                pass
        return " ".join(cue.text for cue in cues)

    def _args(self, prepared: Prepared, out_path: Path, title: str,
              cues: list) -> list[str]:
        args = [
            FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-fflags", "+genpts", "-f", "concat", "-safe", "0",
            "-i", str(prepared.list_path),
        ]

        graph, needs_filter, extra = self._graph(prepared, title, cues)
        for path in extra:
            # 1 fps stills: far less decoder memory than the default rate.
            args += ["-loop", "1", "-framerate", "1", "-i", str(path)]

        args += ["-ss", f"{prepared.offset:.3f}", "-t", f"{prepared.duration:.3f}"]

        if needs_filter:
            args += ["-filter_complex", graph, "-map", "[vout]", "-map", "0:a?"]
        else:
            args += ["-map", "0:v:0", "-map", "0:a?"]

        args += self._encoder_args()
        args += ["-movflags", "+faststart", "-metadata", "comment=clipped live", str(out_path)]
        return args

    def _encoder_args(self) -> list[str]:
        cfg = self.cfg
        args = ["-c:v", self.encoder]
        if self.encoder == "h264_videotoolbox":
            args += ["-b:v", "8M", "-allow_sw", "1"]
        elif self.encoder == "libx264":
            args += ["-preset", cfg.preset, "-crf", str(cfg.crf), "-profile:v", "high"]
        else:
            args += ["-b:v", "12M"]
        if cfg.fps:
            args += ["-r", str(cfg.fps)]
        args += ["-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-ac", "2", "-ar", "48000"]
        return args

    def _graph(self, prepared: Prepared, title: str,
               cues: list) -> tuple[str, bool, list[Path]]:
        cfg = self.cfg
        vertical = cfg.layout != "horizontal"
        style = self.caption_style()
        if cfg.layout == "horizontal" and not title and not cues:
            return "", False, []

        parts: list[str] = []
        if cfg.layout == "vertical":
            parts.append(
                "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
                "crop=1080:1920,setsar=1[vbase]"
            )
        elif cfg.layout == "blur":
            # Blur a quarter-size frame and upscale: identical look, ~16x cheaper.
            parts.append("[0:v]split=2[bg][fg]")
            parts.append(
                "[bg]scale=270:480:force_original_aspect_ratio=increase,"
                "crop=270:480,gblur=sigma=7,scale=1080:1920,"
                "eq=brightness=-0.06[b]"
            )
            parts.append("[fg]scale=1080:1920:force_original_aspect_ratio=decrease[f]")
            parts.append("[b][f]overlay=(W-w)/2:(H-h)/2,setsar=1[vbase]")
        elif cfg.layout == "split":
            parts.extend(split_graph(self.cam_spec(), prepared.width, prepared.height))
        else:
            parts.append("[0:v]setsar=1[vbase]")

        extra: list[Path] = []
        current = "vbase"
        index = 1

        def overlay_png(text: str, path: Path, font_px: int, max_width: int,
                        x: str, y: str, window: tuple[float, float] | None = None,
                        pop: bool = False) -> None:
            nonlocal current, index
            # Real outlined font when ImageMagick is around, bitmap otherwise.
            drawn = None
            if caption.has_styled():
                if pop:
                    drawn = caption.render_pop(text, path, max_width, cap_px=font_px)
                else:
                    drawn = caption.render_styled(text, font_px, path, max_width)
            if drawn is None:
                caption.render(text, font_px, path, max_width)
            extra.append(path)
            label = f"vov{index}"
            enable = (f":enable='between(t,{window[0]:.3f},{window[1]:.3f})'"
                      if window else "")
            parts.append(f"[{current}][{index}:v]overlay=x={x}:y={y}{enable}:format=auto[{label}]")
            current = label
            index += 1

        if title:
            font = find_font()
            if caption.has_drawtext() and font:
                prepared.title_file.write_text(_wrap(title.upper()), encoding="utf-8")
                size = int(1080 * 0.055) if vertical else int(max(320, prepared.height or 720) * 0.072)
                if vertical:
                    y = "h*0.06" if style == "pop" else "h*0.70"
                else:
                    y = "h-text_h-90"
                parts.append(
                    f"[{current}]drawtext="
                    f"fontfile='{font}':textfile='{prepared.title_file.resolve().as_posix()}':"
                    f"fontcolor=white:fontsize={size}:line_spacing=8:"
                    "box=1:boxcolor=black@0.55:boxborderw=26:"
                    f"x=(w-text_w)/2:y={y}:"
                    "shadowcolor=black@0.65:shadowx=2:shadowy=2[vdraw]"
                )
                current = "vdraw"
            else:
                font_px = int(1080 * 0.068) if vertical else int(max(320, prepared.height or 720) * 0.10)
                max_width = int((1080 if vertical else (prepared.width or 1280)) * 0.9)
                x, y = caption.overlay_expr(cfg.layout, style)
                overlay_png(title, prepared.work / "title.png", font_px, max_width, x, y)

        if cues:
            shown = transcribe.pop_chunks(cues) if style == "pop" else cues
            base = 1080 if vertical else max(320, prepared.width or 1280)
            max_width = int((1080 if vertical else (prepared.width or 1280)) * 0.92)
            font_px = int(base * (0.095 if style == "pop" else 0.062))
            x, y = caption.subtitle_expr(cfg.layout, style)
            for position, cue in enumerate(shown):
                overlay_png(
                    cue.text, prepared.work / f"sub_{position:03d}.png",
                    font_px, max_width, x, y, window=(cue.start, cue.end),
                    pop=(style == "pop"),
                )

        parts.append(f"[{current}]null[vout]")
        return ";".join(parts), True, extra


def clip_filename(channel: str, score: float, stamp: str | None = None) -> str:
    stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
    return f"{stamp}_{safe_name(channel, 24)}_{int(round(score * 100)):03d}.mp4"
