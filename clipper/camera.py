"""Find the streamer's webcam from the video itself.

Streamers move their camera around, and a hardcoded webcam box goes stale the
moment they do. The reliable signal is time: the streamer's face is the one
thing that is *always* there. So clipper samples a handful of frames spread over
a few seconds, runs face detection on each, and keeps the face that shows up in
almost every frame. A face that appears once or twice is somebody in the
gameplay and is ignored.

On macOS face detection comes from the Vision framework through a tiny Swift
helper that is compiled once into the work directory and cached. Anywhere else,
or without a Swift toolchain, ``available()`` is False and clipper falls back to
the manual ``--cam x:y:w:h`` box.

Everything here is best-effort: it returns ``None`` rather than raising, because
a guessed camera is a nicety and a crashed loop is not.
"""

from __future__ import annotations

import json
import shutil
import struct
import sys
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .util import FFMPEG, have, run

# Bump when SWIFT_SOURCE changes so the cached binary is rebuilt.
TOOL_VERSION = "3"

# The helper does two jobs for the price of one compile: find faces (for the
# split layout) and measure the lips (for telling the streamer's voice apart
# from whoever else is in the mix). ``-d DIR`` runs it over a whole directory of
# frames in one process, which is what the caption gate needs: spawning a
# process per frame costs more than the detection itself.
SWIFT_SOURCE = r"""
import Foundation
import Vision
import ImageIO

func lipRatio(_ region: VNFaceLandmarkRegion2D?) -> Double {
    guard let pts = region?.normalizedPoints, pts.count > 1 else { return -1 }
    var minX = 1.0, maxX = 0.0, minY = 1.0, maxY = 0.0
    for p in pts {
        minX = min(minX, Double(p.x)); maxX = max(maxX, Double(p.x))
        minY = min(minY, Double(p.y)); maxY = max(maxY, Double(p.y))
    }
    let width = maxX - minX
    if width < 1e-5 { return -1 }
    return (maxY - minY) / width
}

func facesIn(_ url: URL) -> [[String: Double]] {
    guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
          let cg = CGImageSourceCreateImageAtIndex(source, 0, nil) else { return [] }
    let handler = VNImageRequestHandler(cgImage: cg, options: [:])
    let request = VNDetectFaceLandmarksRequest()
    do { try handler.perform([request]) } catch { return [] }
    var out: [[String: Double]] = []
    for obs in (request.results ?? []) {
        let b = obs.boundingBox
        var item: [String: Double] = [
            "x": Double(b.origin.x),
            "y": Double(1.0 - b.origin.y - b.size.height),
            "w": Double(b.size.width),
            "h": Double(b.size.height),
            "conf": Double(obs.confidence),
        ]
        item["outer"] = lipRatio(obs.landmarks?.outerLips)
        item["inner"] = lipRatio(obs.landmarks?.innerLips)
        out.append(item)
    }
    return out
}

func emit(_ value: Any) {
    let data = try! JSONSerialization.data(withJSONObject: value)
    print(String(data: data, encoding: .utf8)!)
}

let args = CommandLine.arguments
guard args.count >= 2 else { emit([]); exit(2) }

if args[1] == "-d" {
    guard args.count >= 3 else { emit([]); exit(2) }
    let dir = URL(fileURLWithPath: args[2])
    let files = (try? FileManager.default.contentsOfDirectory(
        at: dir, includingPropertiesForKeys: nil)) ?? []
    let images = files.filter {
        let ext = $0.pathExtension.lowercased()
        return ext == "png" || ext == "jpg" || ext == "jpeg"
    }.sorted { $0.lastPathComponent < $1.lastPathComponent }
    var batch: [[String: Any]] = []
    for file in images {
        batch.append(["file": file.lastPathComponent, "faces": facesIn(file)])
    }
    emit(batch)
} else {
    emit(facesIn(URL(fileURLWithPath: args[1])))
}
"""

# Sample this many frames, this far apart. Twelve frames a second apart is
# enough to tell "always there" (the streamer) from "walked past" (gameplay),
# while staying under a fifth of the buffer window.
SAMPLE_FRAMES = 12
SAMPLE_FPS = 1.0
MIN_CONF = 0.45

# How the detected face grows into a webcam-shaped box. A webcam frames the head
# in the upper middle with the shoulders below, so the face sits roughly 60%
# across and a head-height from the top. These are deliberately generous and are
# only a starting point — an explicit ``--cam x:y:w:h`` always wins.
CAM_ZOOM = 2.0        # box height as a multiple of the face height
CAM_ASPECT = 1.10     # box width / box height
CAM_HEADROOM = 1.00   # face height between the box top and the eyes
CAM_SIDE = 0.60       # fraction of the box that sits left of the face centre
CAM_MIN_W = 0.12      # never smaller than this fraction of the frame
CAM_MIN_H = 0.16
CAM_MAX_W = 0.46      # never larger — a webcam is an inset, not the frame
CAM_MAX_H = 0.62


@dataclass(frozen=True)
class Face:
    """A face box in normalised coordinates, origin top-left.

    ``outer``/``inner`` are the lip height/width ratios Vision reports, both
    relative to the face box. ``-1`` means the landmarks were not available:
    the mouth gate treats that frame as unknown rather than as a closed mouth.
    """

    x: float
    y: float
    w: float
    h: float
    conf: float = 1.0
    outer: float = -1.0
    inner: float = -1.0

    @property
    def cx(self) -> float:
        return self.x + self.w / 2.0

    @property
    def cy(self) -> float:
        return self.y + self.h / 2.0

    @property
    def area(self) -> float:
        return self.w * self.h


def available() -> bool:
    return sys.platform == "darwin" and (have("swiftc") or have("xcrun"))


@lru_cache(maxsize=2)
def _tool_for(work_dir: str) -> Path | None:
    """Compile the Vision helper once and cache the binary in the work dir."""
    if not available():
        return None
    directory = Path(work_dir)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    binary = directory / "camdetect"
    version = directory / "camdetect.version"
    if (binary.exists() and version.exists()
            and version.read_text().strip() == TOOL_VERSION):
        return binary

    source = directory / "camdetect.swift"
    try:
        source.write_text(SWIFT_SOURCE, encoding="utf-8")
    except OSError:
        return None
    compiler = "swiftc" if have("swiftc") else "xcrun"
    cmd = [compiler, "-O", "-o", str(binary), str(source), "-framework", "Vision"]
    if compiler == "xcrun":
        cmd = [compiler, "swiftc", "-O", "-o", str(binary), str(source),
               "-framework", "Vision"]
    result = run(cmd, timeout=300)
    if result.returncode != 0 or not binary.exists():
        return None
    try:
        version.write_text(TOOL_VERSION + "\n", encoding="utf-8")
    except OSError:
        pass
    return binary


def _png_size(path: Path) -> tuple[int, int]:
    try:
        head = path.read_bytes()[:24]
    except OSError:
        return 0, 0
    if len(head) < 24 or not head.startswith(b"\x89PNG"):
        return 0, 0
    return struct.unpack(">II", head[16:24])


def _face_from_item(item: dict) -> Face | None:
    try:
        face = Face(float(item["x"]), float(item["y"]), float(item["w"]),
                    float(item["h"]), float(item.get("conf", 1.0)),
                    float(item.get("outer", -1.0)),
                    float(item.get("inner", -1.0)))
    except (KeyError, TypeError, ValueError):
        return None
    if face.w <= 0 or face.h <= 0 or face.conf < MIN_CONF:
        return None
    return face


def faces_in_image(tool: Path, image: Path) -> list[Face]:
    """Run face detection on one image. Returns normalised boxes; never raises."""
    try:
        result = run([str(tool), str(image)], timeout=30)
    except Exception:  # noqa: BLE001 - a timeout is just "no faces"
        return []
    if result.returncode != 0:
        return []
    try:
        payload = json.loads(result.stdout or "[]")
    except ValueError:
        return []
    faces: list[Face] = []
    for item in payload:
        face = _face_from_item(item)
        if face is not None:
            faces.append(face)
    return faces


def faces_in_dir(tool: Path, directory: Path, timeout: float = 180.0
                 ) -> list[list[Face]]:
    """Detect faces in every image of a directory, one process for the batch."""
    try:
        result = run([str(tool), "-d", str(directory)], timeout=timeout)
    except Exception:  # noqa: BLE001 - a slow batch is just "no faces"
        return []
    if result.returncode != 0:
        return []
    try:
        payload = json.loads(result.stdout or "[]")
    except ValueError:
        return []
    out: list[list[Face]] = []
    for frame in payload:
        faces: list[Face] = []
        for item in (frame.get("faces") or []) if isinstance(frame, dict) else []:
            face = _face_from_item(item)
            if face is not None:
                faces.append(face)
        out.append(faces)
    return out


def cluster_faces(per_frame: list[list[Face]], tol_x: float = 0.06,
                  tol_y: float = 0.09) -> list[tuple[set[int], Face]]:
    """Group the same face across frames. Returns [(frames_seen, median_face)].

    A detection joins a cluster while its centre is close to the running centre
    and its size is comparable, so a face that drifts slightly stays together
    while a different face at a different spot starts its own cluster.
    """
    clusters: list[dict] = []
    for index, faces in enumerate(per_frame):
        for face in faces:
            target = None
            for cluster in clusters:
                running = cluster["faces"]
                cx = sum(f.cx for f in running) / len(running)
                cy = sum(f.cy for f in running) / len(running)
                if (abs(face.cx - cx) <= tol_x and abs(face.cy - cy) <= tol_y
                        and 0.4 <= face.w / max(1e-6, running[0].w) <= 2.5):
                    target = cluster
                    break
            if target is None:
                clusters.append({"faces": [face], "frames": {index}})
            else:
                target["faces"].append(face)
                target["frames"].add(index)

    out: list[tuple[set[int], Face]] = []
    for cluster in clusters:
        out.append((cluster["frames"], _median(cluster["faces"])))
    out.sort(key=lambda item: (len(item[0]), item[1].conf), reverse=True)
    return out


def _median(faces: list[Face]) -> Face:
    def pick(attr: str) -> float:
        values = sorted(getattr(f, attr) for f in faces)
        return values[len(values) // 2]

    return Face(pick("x"), pick("y"), pick("w"), pick("h"),
                sum(f.conf for f in faces) / len(faces),
                pick("outer"), pick("inner"))


def pick_face(per_frame: list[list[Face]], min_frames: int) -> Face | None:
    """The face that is present in the most frames — the streamer's, in theory."""
    clusters = cluster_faces(per_frame)
    for frames, face in clusters:
        if len(frames) >= min_frames:
            return face
    return None


def expand_to_cam(face: Face, src_w: int, src_h: int) -> tuple[int, int, int, int]:
    """Grow a face box into a webcam-shaped crop, clamped to the frame.

    The face keeps its place inside the box. When the camera sits against the
    bottom or side of the frame (very common — it is a corner inset), the box is
    shrunk to fit instead of being slid up into the gameplay above it. Sliding
    was how a "webcam" box ended up mostly door.
    """
    fw = face.w * src_w
    fh = face.h * src_h
    fx = face.x * src_w
    fy = face.y * src_h

    height = fh * CAM_ZOOM
    height = min(max(height, CAM_MIN_H * src_h), CAM_MAX_H * src_h)

    top = fy - CAM_HEADROOM * fh
    if top < 0:
        top = 0.0
    # Room below the face, not room above it: a webcam at the bottom edge has
    # no space under it, so the crop has to end at the frame.
    height = min(height, src_h - top)
    height = max(height, min(fh * 1.4, src_h))

    width = min(height * CAM_ASPECT, src_w)
    width = min(max(width, CAM_MIN_W * src_w), CAM_MAX_W * src_w)
    for _ in range(2):
        left = (fx + fw / 2.0) - CAM_SIDE * width
        if left < 0:
            left = 0.0
        if left + width > src_w:
            left = max(0.0, src_w - width)
            width = min(width, src_w - left)

    def even(v: float) -> int:
        return max(2, int(round(v)) - int(round(v)) % 2)

    return even(left), even(top), even(width), even(height)


def _ffmpeg_grab(url: str, out_dir: Path, frames: int, fps: float,
                 timeout: float) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
    if url.lower().startswith("http"):
        cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
    if _is_hls(url):
        # A live playlist starts at its oldest segment by default, which on a
        # long buffer means detecting the streamer from a minute-old frame.
        # Sample the newest few segments instead, so the box matches now.
        cmd += ["-live_start_index", "-6"]
    cmd += [
        "-i", url,
        "-vf", f"fps={fps:g}",
        "-frames:v", str(frames),
        str(out_dir / "cam_%03d.png"),
    ]
    try:
        result = run(cmd, timeout=timeout)
    except Exception:  # noqa: BLE001 - a slow stream is just a failed sample
        return sorted(out_dir.glob("cam_*.png"))
    frames_out = sorted(out_dir.glob("cam_*.png"))
    if result.returncode != 0 and not frames_out:
        return []
    return frames_out


def detect(url: str, work_dir: Path, frames: int = SAMPLE_FRAMES,
           fps: float = SAMPLE_FPS, timeout: float = 120.0,
           log=lambda message: None) -> tuple[int, int, int, int] | None:
    """Detect the webcam box in the source. Returns ``x:y:w:h`` pixels or None."""
    scratch = work_dir / "cam"
    try:
        tool = _tool_for(str(work_dir))
        if tool is None:
            return None
        shutil.rmtree(scratch, ignore_errors=True)
        images = _ffmpeg_grab(url, scratch, frames, fps, timeout)
        if len(images) < 2:
            return None
        sampled = faces_in_dir(tool, scratch)
        if len(sampled) < len(images):
            sampled += [[] for _ in range(len(images) - len(sampled))]
        width, height = _png_size(images[0])
        if width <= 0 or height <= 0:
            return None
        min_frames = max(2, int(len(images) * 0.4))
        face = pick_face(sampled, min_frames)
        if face is None:
            return None
        box = expand_to_cam(face, width, height)
        log(f"camera detected at {box[0]}:{box[1]}:{box[2]}:{box[3]} "
            f"(face in {_frames_seen(sampled, face)}/{len(images)} frames)")
        return box
    except Exception:  # noqa: BLE001 - detection is never allowed to crash the loop
        return None
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _frames_seen(per_frame: list[list[Face]], face: Face) -> int:
    count = 0
    for faces in per_frame:
        if any(abs(f.cx - face.cx) <= 0.06 and abs(f.cy - face.cy) <= 0.09
               for f in faces):
            count += 1
    return count


def format_box(box: tuple[int, int, int, int]) -> str:
    return ":".join(str(int(v)) for v in box)


# --------------------------------------------------------------------------- #
# who is talking
# --------------------------------------------------------------------------- #

# Stream audio is one mixed track: the game's dialogue sits on top of the mic.
# The only local signal for "is that the streamer" is the webcam itself, so the
# mouth is measured a few times a second and captions are kept only while it
# moves. Fast enough for word timings, cheap enough to run per clip.
MOUTH_FPS = 6.0
MOUTH_SCALE = 2

# A mouth counts as open when it opens this much past its own recent baseline.
# Lip landmarks are noisy (single-frame spikes are common), so two consecutive
# samples are required in both directions and the result is merged and padded.
MOUTH_OPEN = 0.050
MOUTH_CLOSE = 0.022
MOUTH_MIN_COVERAGE = 0.55   # below this the landmarks are not trustworthy


@dataclass(frozen=True)
class TalkTimeline:
    """When the streamer's mouth was moving, in clip-relative seconds."""

    intervals: tuple[tuple[float, float], ...]
    coverage: float   # fraction of sampled frames with a measurable mouth
    duration: float

    @property
    def activity(self) -> float:
        talked = sum(b - a for a, b in self.intervals)
        return talked / self.duration if self.duration > 0 else 0.0

    def talking(self, start: float, end: float) -> bool:
        """True when the streamer's mouth moves while [start, end] is spoken."""
        span = end - start
        need = min(0.18, max(0.06, span * 0.5))
        for a, b in self.intervals:
            if min(b, end) - max(a, start) >= need:
                return True
        return False


def _is_hls(url: str) -> bool:
    return url.lower().split("?")[0].endswith((".m3u8", ".m3u"))


def _rolling_median(values: list[float | None], half: int) -> list[float | None]:
    out: list[float | None] = []
    for index in range(len(values)):
        window = [v for v in values[max(0, index - half):index + half + 1]
                  if v is not None]
        out.append(sorted(window)[len(window) // 2] if window else None)
    return out


def talking_intervals(ratios: list[float | None], fps: float,
                      duration: float | None = None,
                      min_coverage: float = MOUTH_MIN_COVERAGE
                      ) -> TalkTimeline | None:
    """Turn per-frame lip ratios into "mouth moving" intervals.

    ``ratios`` is one outer-lip height/width value per frame, or None when no
    face was found. The threshold follows a rolling median rather than a fixed
    number: the ratio depends on the camera angle, so a streamer whose resting
    mouth reads high would otherwise be "talking" forever.

    Returns None when too few frames had a usable mouth — the caller should then
    leave the transcript alone rather than delete every caption.
    """
    if not ratios:
        return None
    length = duration if duration and duration > 0 else len(ratios) / fps
    coverage = sum(1 for v in ratios if v is not None) / len(ratios)
    if coverage < min_coverage:
        return None

    clean = _rolling_median(ratios, 1)
    baseline = _rolling_median(clean, max(2, int(round(1.5 * fps))))

    raw: list[tuple[float, float]] = []
    open_run = 0
    close_run = 0
    start = 0.0
    on = False
    for index, value in enumerate(clean):
        base = baseline[index]
        if value is None or base is None:
            continue
        if not on:
            if value >= base + MOUTH_OPEN:
                open_run += 1
                if open_run >= 2:
                    on = True
                    start = max(0.0, (index - 1) / fps)
            else:
                open_run = 0
        else:
            if value <= base + MOUTH_CLOSE:
                close_run += 1
                if close_run >= 2:
                    raw.append((start, min(length, (index - 1) / fps)))
                    on = False
                    close_run = 0
            else:
                close_run = 0
    if on:
        raw.append((start, length))

    merged: list[tuple[float, float]] = []
    for a, b in raw:
        a = max(0.0, a - 0.10)
        b = min(length, b + 0.10)
        if merged and a - merged[-1][1] <= 0.30:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return TalkTimeline(tuple(merged), coverage, length)


def analyse(input_args: list[str], work_dir: Path, cam: str,
            offset: float = 0.0, duration: float | None = None,
            fps: float = MOUTH_FPS, timeout: float = 180.0,
            log=lambda message: None) -> TalkTimeline | None:
    """Measure the webcam mouth across a clip window.

    ``input_args`` is whatever ffmpeg needs before the output options (a plain
    ``-i path`` or a concat list). Returns None when the webcam is unusable, and
    never raises: a missing timeline means "do not filter".
    """
    from .config import parse_cam

    scratch = work_dir / "mouth"
    try:
        tool = _tool_for(str(work_dir))
        if tool is None:
            return None
        try:
            x, y, w, h = parse_cam(cam)
        except ValueError:
            return None
        if w < 32 or h < 32:
            return None
        shutil.rmtree(scratch, ignore_errors=True)
        scratch.mkdir(parents=True, exist_ok=True)
        length = max(0.5, float(duration or 0.0))
        frames = max(4, int(round(length * fps)) + 1)
        vf = (f"fps={fps:g},crop={w}:{h}:{x}:{y},"
              f"scale=iw*{MOUTH_SCALE}:ih*{MOUTH_SCALE}:flags=lanczos")
        cmd = [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
        cmd += list(input_args)
        cmd += ["-ss", f"{max(0.0, offset):.3f}", "-t", f"{length:.3f}",
                "-vf", vf, "-frames:v", str(frames), "-q:v", "4",
                str(scratch / "m_%04d.jpg")]
        try:
            result = run(cmd, timeout=timeout)
        except Exception:  # noqa: BLE001 - a slow decode is just no timeline
            return None
        images = sorted(scratch.glob("m_*.jpg"))
        if len(images) < 4 or (result.returncode != 0 and not images):
            return None
        per_frame = faces_in_dir(tool, scratch)
        ratios: list[float | None] = []
        for faces in per_frame:
            if not faces:
                ratios.append(None)
                continue
            face = max(faces, key=lambda f: f.w * f.h)
            ratios.append(face.outer if face.outer > 0 else None)
        timeline = talking_intervals(ratios, fps, length)
        if timeline is None:
            log(f"caption gate: face in "
                f"{sum(1 for r in ratios if r is not None)}/{len(ratios)} frames "
                f"— webcam too unreliable, keeping every cue")
        return timeline
    except Exception:  # noqa: BLE001 - gating is best-effort, never fatal
        return None
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":  # pragma: no cover - manual check
    import argparse

    parser = argparse.ArgumentParser(description="detect the webcam box")
    parser.add_argument("-i", "--input", required=True)
    parser.add_argument("--work", default=".clipper")
    args = parser.parse_args()
    started = time.time()
    found = detect(args.input, Path(args.work), log=print)
    print("result:", format_box(found) if found else "not found",
          f"({time.time() - started:.1f}s)")
