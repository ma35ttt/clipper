"""Restructure a clip before it renders: cold-open the payoff, then rewind.

A raw clip played start to finish is a reupload. The same clip edited so the
payoff is teased first, then replayed with its context, is an edit — and the
moment lands twice.

The restructuring is deliberately mechanical: word timings decide where the
payoff starts and ends, cut points are pushed into the silence between words so
no word is clipped, and nothing is removed. The cold open is a copy of a span
that plays again later, which is why the finished clip is a few seconds longer
than the raw cut.

Everything here is optional and fails soft: when a plan cannot be built, or
ffmpeg refuses the assembly, the caller renders the clip in its original order.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import transcribe
from .cutter import Prepared
from .util import FFMPEG, probe, run

MIN_DURATION = 10.0      # shorter clips have no room for a tease + rewind
MIN_PAYOFF = 2.0
MIN_REWIND = 2.0         # seconds of context before the payoff
PAD_START = 0.35
PAD_END = 0.45
DIP = 0.10               # dip to black at the rewind, so the cut reads


@dataclass(frozen=True)
class EditPlan:
    """A cold-open span, plus the full clip that replays after it."""

    payoff: tuple[float, float]
    duration: float

    @property
    def cold_len(self) -> float:
        start, end = self.payoff
        return max(0.0, end - start)

    @property
    def total(self) -> float:
        """Finished clip length: the tease, then the whole clip again."""
        return self.cold_len + self.duration

    def spans(self, start: float, end: float) -> list[tuple[float, float]]:
        """Where a source span lands in the edited clip (it may land twice)."""
        low, high = self.payoff
        out: list[tuple[float, float]] = []
        if end > low and start < high:
            out.append((max(0.0, max(start, low) - low),
                        min(self.cold_len, min(end, high) - low)))
        out.append((self.cold_len + start, self.cold_len + end))
        return [(a, b) for a, b in out if b > a]

    def describe(self) -> str:
        low, high = self.payoff
        return (f"cold open {high - low:.1f}s ({low:.1f}-{high:.1f}), "
                f"then the clip from the start")


def _busiest_anchor(words) -> float:
    """The start of the densest four-second run of speech."""
    best, best_count = words[0].start, -1
    for word in words:
        count = sum(1 for other in words
                    if word.start <= other.start <= word.start + 4.0)
        if count > best_count:
            best, best_count = word.start, count
    return best + 1.2


def _move_out_of_word(value: float, words, after: bool) -> float:
    """Nudge a cut point out of a word, into the silence beside it."""
    for word in words:
        if word.start < value < word.end:
            return word.end + 0.05 if after else word.start - 0.05
    return value


def plan(cues, focus: float | None = None,
         duration: float = 0.0) -> EditPlan | None:
    """Build a cold-open plan, or ``None`` when it would not help.

    ``focus`` is the trigger moment in clip-local seconds; it is where the peak
    was, so the payoff is centred on it. Without a focus the busiest stretch of
    speech is used instead.
    """
    duration = float(duration or 0.0)
    if duration < MIN_DURATION:
        return None
    words = list(getattr(cues, "words", ()) or ())
    if not words:
        words = transcribe.word_timings(cues)
    words = sorted((w for w in words if w.end > w.start), key=lambda w: w.start)
    if len(words) < 3:
        return None

    anchor = float(focus) if focus and focus > 0 else _busiest_anchor(words)
    anchor = max(0.0, min(anchor, duration))

    # Start just before the word spoken at the trigger, end a few seconds later
    # at the last word inside that window.
    first = min(words, key=lambda w: abs(w.start - max(0.0, anchor - 0.4)))
    start = first.start - PAD_START
    window_end = max(start, anchor) + 3.0
    inside = [w for w in words if w.start <= window_end and w.end > start]
    if not inside:
        return None
    last = max(inside, key=lambda w: w.end)
    end = min(duration - 0.15, last.end + PAD_END)

    start = max(0.0, _move_out_of_word(start, words, after=False))
    end = min(duration, _move_out_of_word(end, words, after=True))

    if end - start < MIN_PAYOFF:
        return None
    if start < MIN_REWIND or duration - end < 0.2:
        return None
    return EditPlan((round(start, 3), round(end, 3)), round(duration, 3))


def retime(cues, edit: EditPlan):
    """Rebuild a transcript on the edited timeline.

    A word inside the cold open exists twice in the finished clip, so it gets
    two cues: one for the tease and one for the replay.
    """
    words = []
    for word in getattr(cues, "words", ()) or []:
        for start, end in edit.spans(word.start, word.end):
            words.append(transcribe.Cue(start, end, word.text))
    lines = []
    for cue in list(cues):
        for start, end in edit.spans(cue.start, cue.end):
            lines.append(transcribe.Cue(start, end, cue.text))
    words.sort(key=lambda cue: cue.start)
    lines.sort(key=lambda cue: cue.start)
    return transcribe.Cues(lines, words)


def _intermediate_args(encoder: str) -> list[str]:
    """Encode the assembly clean: it is re-encoded once more by the render."""
    if encoder == "libx264":
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
                "-profile:v", "high"]
    if encoder == "h264_videotoolbox":
        return ["-c:v", "h264_videotoolbox", "-b:v", "14M", "-allow_sw", "1"]
    return ["-c:v", encoder, "-b:v", "14M"]


def _graph(edit: EditPlan, offset: float, with_audio: bool) -> str:
    start, end = edit.payoff
    cold = edit.cold_len
    base = float(offset)
    parts = [
        "[0:v]split=2[v1][v2]",
        f"[v1]trim=start={base + start:.3f}:end={base + end:.3f},"
        f"setpts=PTS-STARTPTS,"
        f"fade=t=out:st={max(0.0, cold - DIP):.3f}:d={DIP:.3f}[vp]",
        f"[v2]trim=start={base:.3f}:end={base + edit.duration:.3f},"
        "setpts=PTS-STARTPTS,fade=t=in:st=0:d=0.18[vc]",
    ]
    if not with_audio:
        return ";".join(parts) + ";[vp][vc]concat=n=2:v=1:a=0[vout]"
    parts += [
        "[0:a]asplit=2[a1][a2]",
        f"[a1]atrim=start={base + start:.3f}:end={base + end:.3f},"
        "asetpts=PTS-STARTPTS[ap]",
        f"[a2]atrim=start={base:.3f}:end={base + edit.duration:.3f},"
        "asetpts=PTS-STARTPTS[ac]",
    ]
    return (";".join(parts)
            + ";[vp][ap][vc][ac]concat=n=2:v=1:a=1[vout][aout]")


def assemble(prepared: Prepared, edit: EditPlan, encoder: str = "libx264",
             log=lambda message: None) -> Prepared | None:
    """Write the restructured clip and return a ``Prepared`` that points at it.

    Returns ``None`` on any failure; the caller then renders the original order.
    The returned object reuses ``prepared.work`` so the normal cleanup removes
    both the buffer copy and the intermediate.
    """
    media = prepared.work / "coldopen.mp4"
    try:
        media.unlink()
    except OSError:
        pass

    for with_audio in (True, False):
        args = [
            FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-fflags", "+genpts", "-f", "concat", "-safe", "0",
            "-i", str(prepared.list_path),
            "-filter_complex", _graph(edit, prepared.offset, with_audio),
            "-map", "[vout]",
        ]
        if with_audio:
            args += ["-map", "[aout]", "-c:a", "aac", "-b:a", "192k"]
        args += _intermediate_args(encoder)
        args += ["-pix_fmt", "yuv420p", "-movflags", "+faststart", str(media)]
        result = run(args, timeout=900)
        if result.returncode == 0 and media.exists():
            break
    else:
        detail = (result.stderr or "").strip().splitlines()
        log(f"edit: could not restructure the clip — "
            f"{detail[-1][:160] if detail else 'ffmpeg failed'}")
        return None

    info = probe(media)
    duration = float(info.get("duration") or 0.0)
    if duration <= 0:
        log("edit: the restructured clip has no usable duration")
        return None

    listing = prepared.work / "edit.txt"
    listing.write_text(f"file '{media.resolve().as_posix()}'\n", encoding="utf-8")
    log(f"edit: {edit.describe()} — {duration:.1f}s")
    return Prepared(
        work=prepared.work,
        list_path=listing,
        offset=0.0,
        duration=duration,
        width=prepared.width,
        height=prepared.height,
        title_file=prepared.title_file,
    )