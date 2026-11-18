"""Hook captions without external dependencies.

Most ffmpeg builds ship `drawtext`; many (including Homebrew's default) do not.
So we either use drawtext, or rasterise a bold bitmap caption to a PNG with the
stdlib and overlay it. Either way clips get a caption on every machine.
"""

from __future__ import annotations

import os
import re
import struct
import zlib
from functools import lru_cache
from pathlib import Path

from .util import FFMPEG, find_font, have, run

# 5x7 glyphs, one char per pixel.
FONT: dict[str, tuple[str, ...]] = {
    "A": (".###.", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"),
    "B": ("####.", "#...#", "#...#", "####.", "#...#", "#...#", "####."),
    "C": (".####", "#....", "#....", "#....", "#....", "#....", ".####"),
    "D": ("####.", "#...#", "#...#", "#...#", "#...#", "#...#", "####."),
    "E": ("#####", "#....", "#....", "####.", "#....", "#....", "#####"),
    "F": ("#####", "#....", "#....", "####.", "#....", "#....", "#...."),
    "G": (".###.", "#...#", "#....", "#.###", "#...#", "#...#", ".###."),
    "H": ("#...#", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"),
    "I": ("#####", "..#..", "..#..", "..#..", "..#..", "..#..", "#####"),
    "J": ("..###", "...#.", "...#.", "...#.", "...#.", "#..#.", ".##.."),
    "K": ("#...#", "#..#.", "#.#..", "##...", "#.#..", "#..#.", "#...#"),
    "L": ("#....", "#....", "#....", "#....", "#....", "#....", "#####"),
    "M": ("#...#", "##.##", "#.#.#", "#...#", "#...#", "#...#", "#...#"),
    "N": ("#...#", "##..#", "#.#.#", "#..##", "#...#", "#...#", "#...#"),
    "O": (".###.", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."),
    "P": ("####.", "#...#", "#...#", "####.", "#....", "#....", "#...."),
    "Q": (".###.", "#...#", "#...#", "#...#", "#.#.#", "#..#.", ".##.#"),
    "R": ("####.", "#...#", "#...#", "####.", "#.#..", "#..#.", "#...#"),
    "S": (".####", "#....", "#....", ".###.", "....#", "....#", "####."),
    "T": ("#####", "..#..", "..#..", "..#..", "..#..", "..#..", "..#.."),
    "U": ("#...#", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."),
    "V": ("#...#", "#...#", "#...#", "#...#", "#...#", ".#.#.", "..#.."),
    "W": ("#...#", "#...#", "#...#", "#...#", "#.#.#", "##.##", "#...#"),
    "X": ("#...#", "#...#", ".#.#.", "..#..", ".#.#.", "#...#", "#...#"),
    "Y": ("#...#", "#...#", ".#.#.", "..#..", "..#..", "..#..", "..#.."),
    "Z": ("#####", "....#", "...#.", "..#..", ".#...", "#....", "#####"),
    "0": (".###.", "#...#", "#..##", "#.#.#", "##..#", "#...#", ".###."),
    "1": ("..#..", ".##..", "..#..", "..#..", "..#..", "..#..", ".###."),
    "2": (".###.", "#...#", "....#", "...#.", "..#..", ".#...", "#####"),
    "3": ("#####", "...#.", "..#..", "...#.", "....#", "#...#", ".###."),
    "4": ("...#.", "..##.", ".#.#.", "#..#.", "#####", "...#.", "...#."),
    "5": ("#####", "#....", "####.", "....#", "....#", "#...#", ".###."),
    "6": ("..##.", ".#...", "#....", "####.", "#...#", "#...#", ".###."),
    "7": ("#####", "....#", "...#.", "..#..", ".#...", ".#...", ".#..."),
    "8": (".###.", "#...#", "#...#", ".###.", "#...#", "#...#", ".###."),
    "9": (".###.", "#...#", "#...#", ".####", "....#", "...#.", ".##.."),
    "!": ("..#..", "..#..", "..#..", "..#..", "..#..", ".....", "..#.."),
    "?": (".###.", "#...#", "....#", "...#.", "..#..", ".....", "..#.."),
    ".": (".....", ".....", ".....", ".....", ".....", ".##..", ".##.."),
    ",": (".....", ".....", ".....", ".....", ".##..", ".##..", ".#..."),
    ":": (".....", ".##..", ".##..", ".....", ".##..", ".##..", "....."),
    "-": (".....", ".....", ".....", "#####", ".....", ".....", "....."),
    "'": ("..#..", "..#..", ".....", ".....", ".....", ".....", "....."),
    "/": ("....#", "...#.", "...#.", "..#..", ".#...", ".#...", "#...."),
    "%": ("##..#", "##.#.", "..#..", ".#...", "#..##", "...##", "....."),
    "+": (".....", "..#..", "..#..", "#####", "..#..", "..#..", "....."),
    "&": (".##..", "#..#.", "#.#..", ".#...", "#.#.#", "#..#.", ".##.#"),
}

ADVANCE = 6  # glyph width + 1 cell gap


@lru_cache(maxsize=1)
def has_drawtext() -> bool:
    p = run([FFMPEG, "-hide_banner", "-filters"], timeout=30)
    return "drawtext" in (p.stdout or "")


# --------------------------------------------------------------------------- #
# real-font captions via ImageMagick (optional, auto-detected)
#
# The bitmap font above is the zero-dependency floor. When ImageMagick is
# installed we render proper outlined text with a real system font instead,
# which is what makes a clip look like a grown-up Short rather than a debug
# build. Everything degrades to the bitmap path if `magick` is missing.
# --------------------------------------------------------------------------- #

STYLED_FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Tahoma Bold.ttf",
    "/System/Library/Fonts/Supplemental/Verdana Bold.ttf",
    "/System/Library/Fonts/Supplemental/Trebuchet MS Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Black.ttf",
    "/System/Library/Fonts/Supplemental/Arial Rounded Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
)


@lru_cache(maxsize=1)
def magick_binary() -> str | None:
    for name in ("magick", "convert"):
        if have(name):
            return name
    return None


@lru_cache(maxsize=1)
def styled_font() -> str | None:
    override = os.environ.get("CLIPPER_FONT")
    if override and Path(override).exists():
        return override
    for cand in STYLED_FONT_CANDIDATES:
        if Path(cand).exists():
            return cand
    return find_font()


@lru_cache(maxsize=1)
def has_styled() -> bool:
    return bool(magick_binary() and styled_font())


def _png_size(path: Path) -> tuple[int, int]:
    try:
        head = path.read_bytes()[:24]
    except OSError:
        return 0, 0
    if len(head) < 24 or not head.startswith(b"\x89PNG"):
        return 0, 0
    return struct.unpack(">II", head[16:24])


def _label_png(binary: str, font: str, px: int, text: str, path: Path,
               stroke_px: int, fill: str = "white") -> bool:
    """Render text with a true *outside* black outline to ``path``.

    ImageMagick centres a stroke on the glyph edge, so the usual
    ``-fill white -stroke black`` eats into the letters and a normal bold face
    comes out thin and hollow. Instead the alpha channel is dilated into a black
    silhouette and the untouched fill (white by default, gold for the hook) is
    composited on top. One process, one image, and the letters keep their full
    weight.
    """
    margin = stroke_px * 2 + 4
    result = run([
        binary, "-background", "none", "-fill", fill, "-stroke", "none",
        "-font", font, "-pointsize", str(px), f"label:{text}",
        "-bordercolor", "none", "-border", str(margin),
        "(", "+clone", "-channel", "A", "-morphology", "Dilate",
        f"Disk:{stroke_px}", "+channel", "-fill", "black", "-colorize", "100",
        ")",
        "+swap", "-compose", "over", "-composite",
        "-trim", "+repage", f"PNG32:{path}",
    ], timeout=30)
    return result.returncode == 0 and path.exists()


def render_styled(text: str, font_px: int, path: Path, max_width: int,
                  max_lines: int = 2, font: str | None = None,
                  fill: str = "white", char_width: float = 0.56) -> tuple[int, int] | None:
    """Heavy text with a thick black outline on a transparent PNG.

    ``font``/``fill`` let the hook use its own face and colour while the
    subtitles stay white. Returns (width, height), or None when ImageMagick is
    unavailable or the render failed, so the caller can fall back to the bitmap
    font.

    Each line is rendered on its own and the lines are stacked centred: passing
    a multi-line ``label:`` together with ``-gravity`` makes ImageMagick drop
    the fill on the second line, which looks like hollow text.
    """
    binary = magick_binary()
    font = font or styled_font()
    if not binary or not font or not text.strip():
        return None
    # Arial Bold averages ~0.56em per character; Arial Black ~0.72em. Aim just
    # under max_width.
    per_line = max(6, int(max_width / (font_px * char_width)))
    lines = [line for line in wrap(text, per_line, max_lines) if line.strip()]
    if not lines:
        return None
    stroke = max(2, int(round(font_px * 0.09)))
    pieces: list[Path] = []
    try:
        for index, line in enumerate(lines):
            piece = path.with_name(f"{path.stem}_line{index}.png")
            if not _label_png(binary, font, font_px, line, piece, stroke, fill):
                return None
            pieces.append(piece)

        stack = [binary, "-background", "none", "-gravity", "center"]
        stack += [str(piece) for piece in pieces]
        stack += ["-append", "-trim", "+repage", f"PNG32:{path}"]
        result = run(stack, timeout=30)
        if result.returncode != 0 or not path.exists():
            return None
    except Exception:  # noqa: BLE001 - never let captions break a clip
        return None
    finally:
        for piece in pieces:
            try:
                piece.unlink()
            except OSError:
                pass
    width, height = _png_size(path)
    if width <= 0 or height <= 0:
        return None
    return width, height


def sanitize(text: str) -> str:
    out = []
    for ch in text.upper():
        if ch == " " or ch in FONT:
            out.append(ch)
        elif ch in "\n\t":
            out.append(" ")
        else:
            out.append("?")
    return "".join(out)


def wrap(text: str, max_chars: int, max_lines: int = 3) -> list[str]:
    words: list[str] = []
    for line in sanitize(text).splitlines():
        words.extend(line.split())
    lines: list[str] = []
    current = ""
    for word in words:
        if current and len(current) + 1 + len(word) > max_chars:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines[:max_lines] or [""]


def _png(path: Path, width: int, height: int, pixels: bytearray) -> None:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    raw = bytearray()
    stride = width * 4
    for y in range(height):
        raw.append(0)
        raw += pixels[y * stride:(y + 1) * stride]
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
           + chunk(b"IEND", b""))
    path.write_bytes(png)


def render(text: str, font_px: int, path: Path, max_width: int,
           box_alpha: int = 150) -> tuple[int, int]:
    """Rasterise a caption to an RGBA PNG. Returns (width, height)."""
    cell = max(3, int(round(font_px / 7)))
    pad = cell * 2
    gap = int(cell * 1.1)
    per_line = max(6, (max_width - 2 * pad) // (ADVANCE * cell))
    lines = wrap(text, per_line)
    widths = [max(1, len(line) * ADVANCE * cell - cell) for line in lines]
    width = max(widths) + 2 * pad
    height = len(lines) * 7 * cell + (len(lines) - 1) * gap + 2 * pad

    pixels = bytearray(width * height * 4)
    for i in range(width * height):
        pixels[i * 4 + 3] = box_alpha  # black box, semi-transparent

    for row, line in enumerate(lines):
        y0 = pad + row * (7 * cell + gap)
        for index, ch in enumerate(line):
            glyph = FONT.get(ch)
            if not glyph:
                continue
            gx = pad + index * ADVANCE * cell
            for gy, bits in enumerate(glyph):
                for bx, bit in enumerate(bits):
                    if bit != "#":
                        continue
                    px = gx + bx * cell
                    py = y0 + gy * cell
                    for dy in range(cell):
                        base = ((py + dy) * width + px) * 4
                        for dx in range(cell):
                            off = base + dx * 4
                            pixels[off:off + 4] = b"\xff\xff\xff\xff"
    _png(path, width, height, pixels)
    return width, height


# --------------------------------------------------------------------------- #
# popping captions (the bold word-chunks every clipper uses)
#
# One short chunk of words, white with a heavy black outline, on a rounded dark
# pill, swapped for the next chunk as it is spoken. ImageMagick draws it; the
# bitmap block above is the fallback when it is missing.
# --------------------------------------------------------------------------- #

POP_FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Tahoma Bold.ttf",
    "/System/Library/Fonts/Supplemental/Verdana Bold.ttf",
    "/System/Library/Fonts/Supplemental/Trebuchet MS Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Narrow Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Black.ttf",
    "/System/Library/Fonts/Supplemental/Arial Rounded Bold.ttf",
    "/Library/Fonts/Arial Black.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
)

# ImageMagick reads a leading '@' as "load this text from a file", and '%' has
# meaning in some contexts; keep only characters that always draw literally.
POP_SAFE = set(" ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.,!?'-:")


@lru_cache(maxsize=1)
def pop_font() -> str | None:
    override = os.environ.get("CLIPPER_FONT")
    if override and Path(override).exists():
        return override
    for cand in POP_FONT_CANDIDATES:
        if Path(cand).exists():
            return cand
    return styled_font()


# The hook is a title, not dialogue: give it a heavier face and a warm colour so
# it reads as a caption about the clip instead of a line the streamer said.
HOOK_FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial Black.ttf",
    "/System/Library/Fonts/Supplemental/Impact.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Black.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "C:/Windows/Fonts/ariblk.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
)

HOOK_FILL = "#FFD23F"


@lru_cache(maxsize=1)
def hook_font() -> str | None:
    override = os.environ.get("CLIPPER_HOOK_FONT")
    if override and Path(override).exists():
        return override
    for cand in HOOK_FONT_CANDIDATES:
        if Path(cand).exists():
            return cand
    return styled_font()


def pop_text(text: str) -> str:
    cleaned = "".join(ch if ch in POP_SAFE else " " for ch in text.upper())
    return re.sub(r"\s+", " ", cleaned).strip()


def render_pop(text: str, path: Path, max_width: int, cap_px: int,
               fill: str = "white", pill: str = "rgba(0,0,0,0.55)",
               min_px: int = 40) -> tuple[int, int] | None:
    """Draw one popping caption chunk to an RGBA PNG.

    Returns (width, height), or None when ImageMagick is unavailable or the
    render failed, so the caller can fall back to the bitmap font. The text is
    measured before the pill is drawn, so a long chunk can never overflow it.
    """
    binary = magick_binary()
    font = pop_font()
    label = pop_text(text)
    if not binary or not font or not label:
        return None

    # Shrink the type until the chunk (text plus pill padding) fits the frame
    # width; short chunks get the cap so "NO" and "WHAT IS HAPPENING" both look
    # intentional.
    estimate = int(max_width / (len(label) * 0.62 + 0.8))
    px = max(min_px, min(cap_px, estimate))
    stroke = max(2, int(round(px * 0.075)))

    piece = path.with_name(f"{path.stem}_text.png")
    try:
        if not _label_png(binary, font, px, label, piece, stroke, fill):
            return None
        text_w, text_h = _png_size(piece)
        if text_w <= 0 or text_h <= 0:
            return None

        pad = int(px * 0.32)
        width, height = text_w + 2 * pad, text_h + 2 * pad
        radius = height // 2
        composed = run([
            binary, "-size", f"{width}x{height}", "xc:none",
            "-fill", pill, "-stroke", "none",
            "-draw", f"roundrectangle 0,0,{width - 1},{height - 1},{radius},{radius}",
            str(piece), "-gravity", "center", "-composite", f"PNG32:{path}",
        ], timeout=30)
        if composed.returncode != 0 or not path.exists():
            return None
    except Exception:  # noqa: BLE001 - captions must never break a clip
        return None
    finally:
        try:
            piece.unlink()
        except OSError:
            pass
    out_w, out_h = _png_size(path)
    return (out_w, out_h) if out_w > 0 and out_h > 0 else None


def overlay_expr(layout: str = "vertical", style: str = "block") -> tuple[str, str]:
    """Position of the hook caption for a given layout.

    With popping subtitles the hook moves to the top so the two never fight for
    the same middle band; the split layout keeps its dedicated filler band.
    """
    x = "(main_w-overlay_w)/2"
    if layout == "horizontal":
        y = "main_h-overlay_h-90"
    elif layout == "split":
        # Under the subtitle block, in the filler band below the gameplay.
        y = "main_h*0.905-overlay_h/2"
    elif style == "pop":
        y = "main_h*0.085"
    else:
        y = "main_h*0.66"
    return x, y


def subtitle_expr(layout: str = "vertical", style: str = "block") -> tuple[str, str]:
    """Subtitles sit lower than the hook caption so the two never collide."""
    x = "(main_w-overlay_w)/2"
    if layout == "horizontal":
        y = "main_h-overlay_h-64"
    elif layout == "split":
        # Immediately under the gameplay block, which ends ~71% down the frame.
        y = ("main_h*0.79-overlay_h/2" if style == "pop"
             else "main_h*0.765-overlay_h/2")
    elif style == "pop":
        # Lower-middle, the classic Shorts caption band.
        y = "main_h*0.70-overlay_h/2"
    else:
        y = "main_h*0.80"
    return x, y
