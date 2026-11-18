"""Per-segment loudness analysis.

We measure the completed HLS segments themselves rather than opening the source
a second time. That means one network connection for the whole tool, and the
audio clock lines up perfectly with the video clock.
"""

from __future__ import annotations

import queue
import re
import threading
from pathlib import Path

from .util import FFMPEG, run

_MEAN = re.compile(r"mean_volume:\s*(-?\d+(?:\.\d+)?) dB")
_MAX = re.compile(r"max_volume:\s*(-?\d+(?:\.\d+)?) dB")

# Stream audio is loud and bass-heavy: raw level mostly reports how much rumble
# and game boom there is, which is always high. Band-limit to the speech band
# before measuring, so the number moves when a *voice* moves.
SPEECH_BAND = "highpass=f=200,lowpass=f=3400"


def measure(path: Path, timeout: float = 30.0) -> tuple[float, float] | None:
    """Return (mean_dB, max_dB) for an audio segment, in the speech band."""
    if not path.exists():
        return None
    p = run(
        [
            FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "info",
            "-i", str(path), "-vn", "-af", f"{SPEECH_BAND},volumedetect",
            "-f", "null", "-",
        ],
        timeout=timeout,
    )
    text = p.stderr or ""
    mean = _MEAN.search(text)
    peak = _MAX.search(text)
    if not mean:
        return None
    return float(mean.group(1)), float(peak.group(1)) if peak else float(mean.group(1))


class LoudnessAnalyzer(threading.Thread):
    """Consumes (seq, path) jobs, emits (seq, mean, peak) results."""

    def __init__(self, jobs: "queue.Queue", results: "queue.Queue"):
        super().__init__(daemon=True, name="loudness")
        self.jobs = jobs
        self.results = results
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()
        try:
            self.jobs.put_nowait(None)
        except queue.Full:
            pass

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                job = self.jobs.get(timeout=0.25)
            except queue.Empty:
                continue
            if job is None:
                return
            seq, path = job
            try:
                measured = measure(Path(path))
            except Exception:
                measured = None
            if measured:
                self.results.put((seq, measured[0], measured[1]))
