"""Rolling livestream buffer backed by ffmpeg's HLS muxer.

The muxer owns the window (it deletes old segments), and the playlist gives us
exact segment durations, so our timeline never drifts from the real stream.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Segment:
    seq: int
    name: str
    path: Path
    duration: float
    start: float  # stream clock, seconds since ingest start
    end: float

    def overlaps(self, a: float, b: float) -> bool:
        return self.end > a + 1e-6 and self.start < b - 1e-6


def parse_playlist(text: str) -> tuple[int, list[tuple[float, str]]]:
    seq = 0
    items: list[list] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                seq = int(line.split(":", 1)[1])
            except ValueError:
                pass
        elif line.startswith("#EXTINF:"):
            try:
                dur = float(line[8:].split(",", 1)[0])
            except ValueError:
                continue
            items.append([dur, None])
        elif not line.startswith("#") and items and items[-1][1] is None:
            items[-1][1] = line
    return seq, [(float(d), str(n)) for d, n in items if n]


class HLSBuffer:
    """Tracks the ffmpeg HLS window and maps segments onto a monotonic stream clock."""

    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.playlist = self.dir / "index.m3u8"
        self._starts: dict[int, float] = {}
        self._durs: dict[int, float] = {}
        self._end = 0.0
        self._newest = -1
        self._first = -1
        self._segments: list[Segment] = []
        self._mtime = 0.0
        self._restarts = 0

    # -- state ---------------------------------------------------------
    @property
    def stream_now(self) -> float | None:
        """End of the newest *complete* segment, or None before ingest is live."""
        return self._end if self._newest >= 0 else None

    @property
    def segments(self) -> list[Segment]:
        return self._segments

    @property
    def restarts(self) -> int:
        return self._restarts

    def between(self, a: float, b: float) -> list[Segment]:
        return [s for s in self._segments if s.overlaps(a, b)]

    # -- polling -------------------------------------------------------
    def poll(self) -> bool:
        """Re-read the playlist. Returns True when new segments appeared."""
        try:
            stat = self.playlist.stat()
        except OSError:
            return False
        if stat.st_mtime == self._mtime:
            return False
        self._mtime = stat.st_mtime
        try:
            text = self.playlist.read_text(errors="replace")
        except OSError:
            return False

        seq0, items = parse_playlist(text)
        if not items:
            return False

        # ffmpeg restarted -> sequence numbers reset/rewind. Rebuild the clock.
        if self._first >= 0 and seq0 < self._first:
            self._reset()
        if seq0 + len(items) - 1 < self._newest:
            self._reset()

        added = False
        for offset, (dur, name) in enumerate(items):
            seq = seq0 + offset
            if seq in self._durs:
                continue
            self._starts[seq] = self._end
            self._durs[seq] = dur
            self._end += dur
            self._newest = max(self._newest, seq)
            added = True
        self._first = seq0

        self._segments = [
            Segment(
                seq=seq0 + offset,
                name=name,
                path=self.dir / name,
                duration=self._durs[seq0 + offset],
                start=self._starts[seq0 + offset],
                end=self._starts[seq0 + offset] + self._durs[seq0 + offset],
            )
            for offset, (_, name) in enumerate(items)
            if (seq0 + offset) in self._starts
        ]
        return added

    def _reset(self) -> None:
        self._starts.clear()
        self._durs.clear()
        self._segments = []
        self._end = 0.0
        self._newest = -1
        self._first = -1
        self._restarts += 1
