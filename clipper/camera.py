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
TOOL_VERSION = "2"

SWIFT_SOURCE = r"""
import Foundation
import Vision
import ImageIO

let args = CommandLine.arguments
guard args.count >= 2 else { print("[]"); exit(2) }
let url = URL(fileURLWithPath: args[1])
guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
      let cg = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
    print("[]")
    exit(0)
}
let handler = VNImageRequestHandler(cgImage: cg, options: [:])
let request = VNDetectFaceRectanglesRequest()
do { try handler.perform([request]) } catch { print("[]"); exit(0) }
var out: [[String: Double]] = []
for obs in (request.results ?? []) {
    let b = obs.boundingBox
    out.append([
        "x": Double(b.origin.x),
        "y": Double(1.0 - b.origin.y - b.size.height),
        "w": Double(b.size.width),
        "h": Double(b.size.height),
        "conf": Double(obs.confidence),
    ])
}
let data = try! JSONSerialization.data(withJSONObject: out)
print(String(data: data, encoding: .utf8)!)
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
CAM_ZOOM = 3.4        # box height as a multiple of the face height
CAM_ASPECT = 1.10     # box width / box height
CAM_HEADROOM = 0.95   # face height between the box top and the eyes
CAM_SIDE = 0.60       # fraction of the box that sits left of the face centre
CAM_MIN_W = 0.12      # never smaller than this fraction of the frame
CAM_MIN_H = 0.16
CAM_MAX_W = 0.46      # never larger — a webcam is an inset, not the frame
CAM_MAX_H = 0.62


@dataclass(frozen=True)
class Face:
    """A face box in normalised coordinates, origin top-left."""

    x: float
    y: float
    w: float
    h: float
    conf: float = 1.0

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
        try:
            face = Face(float(item["x"]), float(item["y"]), float(item["w"]),
                        float(item["h"]), float(item.get("conf", 1.0)))
        except (KeyError, TypeError, ValueError):
            continue
        if face.w <= 0 or face.h <= 0 or face.conf < MIN_CONF:
            continue
        faces.append(face)
    return faces


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
                sum(f.conf for f in faces) / len(faces))


def pick_face(per_frame: list[list[Face]], min_frames: int) -> Face | None:
    """The face that is present in the most frames — the streamer's, in theory."""
    clusters = cluster_faces(per_frame)
    for frames, face in clusters:
        if len(frames) >= min_frames:
            return face
    return None


def expand_to_cam(face: Face, src_w: int, src_h: int) -> tuple[int, int, int, int]:
    """Grow a face box into a webcam-shaped crop, clamped to the frame."""
    fw = face.w * src_w
    fh = face.h * src_h
    fx = face.x * src_w
    fy = face.y * src_h

    height = fh * CAM_ZOOM
    width = height * CAM_ASPECT
    height = min(max(height, CAM_MIN_H * src_h), CAM_MAX_H * src_h)
    width = min(max(width, CAM_MIN_W * src_w), CAM_MAX_W * src_w)

    top = fy - CAM_HEADROOM * fh
    left = (fx + fw / 2.0) - CAM_SIDE * width

    left = max(0.0, min(left, src_w - width))
    top = max(0.0, min(top, src_h - height))

    def even(v: float) -> int:
        return max(2, int(round(v)) - int(round(v)) % 2)

    return even(left), even(top), even(width), even(height)


def _ffmpeg_grab(url: str, out_dir: Path, frames: int, fps: float,
                 timeout: float) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
    if url.lower().startswith("http"):
        cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
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
        sampled: list[list[Face]] = []
        for image in images:
            sampled.append(faces_in_image(tool, image))
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
