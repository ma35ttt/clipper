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

from . import emoji as emoji_mod
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


@lru_cache(maxsize=1)
def ass_available() -> bool:
    """True when this ffmpeg can burn ``.ass`` subtitles (needs libass).

    Homebrew's default ``ffmpeg`` bottle omits libass; ``ffmpeg-full`` is
    keg-only. ``util`` prefers that build when it exists, and callers fall back
    to the PNG popping captions when this returns False.
    """
    p = run([FFMPEG, "-hide_banner", "-filters"], timeout=30)
    text = p.stdout or ""
    return any(line.split()[1:2] == ["ass"] or line.split()[1:2] == ["subtitles"]
               for line in text.splitlines() if len(line.split()) > 2)


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


# A hook reads better with one emoji from the audience's own reaction
# ("COOKED 🔥"). Anything outside these ranges stays plain text: punctuation
# like © or ™ is not emoji and should not drag the emoji renderer in.
EMOJI_SPANS = (
    (0x1F000, 0x1FAFF),   # pictographs, emoticons, symbols, skin tones
    (0x1F1E6, 0x1F1FF),   # flags
    (0x2600, 0x27BF),     # misc symbols, dingbats (☠ ✅ ➡)
    (0x2B00, 0x2BFF),     # stars and arrows
)
EMOJI_JOINERS = {0x200D, 0xFE0E, 0xFE0F, 0x20E3}
EMOJI_FONT_CANDIDATES = (
    "/System/Library/Fonts/Apple Color Emoji.ttc",
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/noto-emoji/NotoColorEmoji.ttf",
    "C:/Windows/Fonts/seguiemj.ttf",
)


def is_emoji(ch: str) -> bool:
    code = ord(ch)
    if code in EMOJI_JOINERS:
        return True
    return any(low <= code <= high for low, high in EMOJI_SPANS)


def has_emoji(text: str) -> bool:
    return any(is_emoji(ch) for ch in text)


def strip_emoji(text: str) -> str:
    kept = "".join("" if is_emoji(ch) else ch for ch in text)
    return re.sub(r"\s+", " ", kept).strip()


def runs(text: str) -> list[tuple[str, bool]]:
    """Split text into (chunk, is_emoji) runs so each can use its own font."""
    out: list[tuple[str, bool]] = []
    buf, flag = "", None
    for ch in text:
        emoji = is_emoji(ch)
        if flag is None or emoji == flag:
            buf += ch
            flag = emoji
        else:
            out.append((buf, bool(flag)))
            buf, flag = ch, emoji
    if buf:
        out.append((buf, bool(flag)))
    return out


@lru_cache(maxsize=1)
def emoji_font() -> str | None:
    override = os.environ.get("CLIPPER_EMOJI_FONT")
    if override and Path(override).exists():
        return override
    for cand in EMOJI_FONT_CANDIDATES:
        if Path(cand).exists():
            return cand
    return None


@lru_cache(maxsize=1)
def has_styled() -> bool:
    return bool(magick_binary() and styled_font())


def can_render_emoji() -> bool:
    """True when a real hook PNG can carry emoji instead of dropping them."""
    return bool(magick_binary() and styled_font() and emoji_font())


def _png_size(path: Path) -> tuple[int, int]:
    try:
        head = path.read_bytes()[:24]
    except OSError:
        return 0, 0
    if len(head) < 24 or not head.startswith(b"\x89PNG"):
        return 0, 0
    return struct.unpack(">II", head[16:24])


def _label_png(binary: str, font: str, px: int, text: str, path: Path,
               stroke_px: int, fill: str = "white", pad: int = 0) -> bool:
    """Render text with a true *outside* black outline to ``path``.

    ImageMagick centres a stroke on the glyph edge, so the usual
    ``-fill white -stroke black`` eats into the letters and a normal bold face
    comes out thin and hollow. Instead the alpha channel is dilated into a black
    silhouette and the untouched fill (white by default, gold for the hook) is
    composited on top. One process, one image, and the letters keep their full
    weight.

    ``pad`` adds transparent pixels around the trimmed result, which is how
    neighbouring emoji and words get a little breathing room when a row is
    assembled.
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
        "-trim", "+repage",
    ] + (["-bordercolor", "none", "-border", str(pad)] if pad else [])
      + [f"PNG32:{path}"], timeout=30)
    return result.returncode == 0 and path.exists()


# Apple Color Emoji only exposes its fixed bitmap strikes; anything between
# them fails to load. Render at a strike that exists and scale down.
EMOJI_STRIKES = (160, 96, 64, 48, 40, 32, 20)


def _colour_emoji_png(binary: str, font: str, font_px: int, text: str,
                      path: Path, stroke_px: int, pad: int = 0) -> bool:
    """Draw emoji from the font's own PNG bitmaps: the real iPhone colours.

    Each glyph is scaled to sit level with the hook text, given the same black
    outline as the letters (the outline is a dilated alpha mask behind the
    untouched colour image), then the run is joined into one strip.
    """
    pieces = emoji_mod.digest(font, text)
    if not pieces:
        return False
    target = max(12, int(round(font_px * 1.02)))
    margin = stroke_px * 2 + 4
    made: list[Path] = []
    try:
        for index, (_, data) in enumerate(pieces):
            raw = path.with_name(f"{path.stem}_r{index}.png")
            raw.write_bytes(data)
            piece = path.with_name(f"{path.stem}_c{index}.png")
            command = [
                binary, "-background", "none", str(raw),
                "-resize", f"x{target}",
                "-bordercolor", "none", "-border", str(margin),
                "(", "+clone", "-channel", "A", "-morphology", "Dilate",
                f"Disk:{stroke_px}", "+channel", "-fill", "black",
                "-colorize", "100", ")",
                "+swap", "-compose", "over", "-composite",
                "-trim", "+repage",
            ]
            if pad:
                command += ["-bordercolor", "none", "-border", str(pad)]
            command += [f"PNG32:{piece}"]
            if run(command, timeout=30).returncode != 0 or not piece.exists():
                return False
            made.append(piece)
        row = [binary, "-background", "none", "-gravity", "center"]
        row += [str(piece) for piece in made]
        row += ["+append", "-trim", "+repage", f"PNG32:{path}"]
        return run(row, timeout=30).returncode == 0 and path.exists()
    finally:
        for piece in made:
            try:
                piece.unlink()
            except OSError:
                pass
        for raw in path.parent.glob(f"{path.stem}_r*.png"):
            try:
                raw.unlink()
            except OSError:
                pass


def _emoji_png(binary: str, font: str, font_px: int, text: str, path: Path,
               stroke_px: int, fill: str, pad: int = 0) -> bool:
    """One emoji run, outlined and scaled to sit on the hook's baseline."""
    if _colour_emoji_png(binary, font, font_px, text, path, stroke_px, pad):
        return True
    # A font with no sbix bitmaps (or an unreadable one): draw the glyph and
    # tint it, which at least keeps the emoji's shape.
    target = max(12, int(round(font_px * 1.02)))
    for strike in EMOJI_STRIKES:
        margin = stroke_px * 2 + 4
        result = run([
            binary, "-background", "none", "-fill", fill, "-stroke", "none",
            "-font", font, "-pointsize", str(strike), f"label:{text}",
            "-resize", f"x{target}",
            # Apple's bitmap emoji comes back as a soft alpha mask, so a gold
            # fill composites over the black outline as muddy brown. Snap the
            # mask opaque: the result is the same flat gold-and-outline look
            # the hook text has.
            "-channel", "A", "-threshold", "8%", "+channel",
            "-bordercolor", "none", "-border", str(margin),
            "(", "+clone", "-channel", "A", "-morphology", "Dilate",
            f"Disk:{stroke_px}", "+channel", "-fill", "black", "-colorize", "100",
            ")",
            "+swap", "-compose", "over", "-composite",
            "-trim", "+repage",
        ] + (["-bordercolor", "none", "-border", str(pad)] if pad else [])
          + [f"PNG32:{path}"], timeout=30)
        if result.returncode == 0 and path.exists():
            return True
    return False


def render_styled(text: str, font_px: int, path: Path, max_width: int,
                  max_lines: int = 2, font: str | None = None,
                  fill: str = "white", char_width: float = 0.56,
                  emoji: bool = False) -> tuple[int, int] | None:
    """Heavy text with a thick black outline on a transparent PNG.

    ``font``/``fill`` let the hook use its own face and colour while the
    subtitles stay white. Returns (width, height), or None when ImageMagick is
    unavailable or the render failed, so the caller can fall back to the bitmap
    font.

    Each line is rendered on its own and the lines are stacked centred: passing
    a multi-line ``label:`` together with ``-gravity`` makes ImageMagick drop
    the fill on the second line, which looks like hollow text.

    With ``emoji=True`` a line is split into text and emoji runs, each drawn in
    a font that actually has the glyph, then joined into one row. Arial has no
    emoji, so without this the characters silently vanish (or become ``?``).
    """
    binary = magick_binary()
    font = font or styled_font()
    if not binary or not font or not text.strip():
        return None
    # Arial Bold averages ~0.56em per character; Arial Black ~0.72em. Aim just
    # under max_width.
    per_line = max(6, int(max_width / (font_px * char_width)))
    lines = [line for line in wrap(text, per_line, max_lines, emoji=emoji)
             if line.strip()]
    if not lines:
        return None
    stroke = max(2, int(round(font_px * 0.09)))
    emoji_face = emoji_font() if emoji else None
    pieces: list[Path] = []
    try:
        for index, line in enumerate(lines):
            row_parts: list[Path] = []
            for part_index, (chunk, is_emoji_run) in enumerate(runs(line)):
                if is_emoji_run and emoji_face:
                    part = path.with_name(f"{path.stem}_l{index}e{part_index}.png")
                    if not _emoji_png(binary, emoji_face, font_px, chunk, part,
                                      stroke, fill, pad=4):
                        continue
                elif chunk.strip():
                    part = path.with_name(f"{path.stem}_l{index}t{part_index}.png")
                    if not _label_png(binary, font, font_px, chunk, part,
                                      stroke, fill, pad=4):
                        return None
                else:
                    continue
                row_parts.append(part)
            if not row_parts:
                return None
            piece = path.with_name(f"{path.stem}_line{index}.png")
            row = [binary, "-background", "none", "-gravity", "center"]
            row += [str(part) for part in row_parts]
            row += ["+append", "-trim", "+repage", f"PNG32:{piece}"]
            if run(row, timeout=30).returncode != 0 or not piece.exists():
                return None
            pieces.append(piece)
            for part in row_parts:
                try:
                    part.unlink()
                except OSError:
                    pass

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


def sanitize(text: str, emoji: bool = False) -> str:
    out = []
    for ch in text.upper():
        if ch == " " or ch in FONT:
            out.append(ch)
        elif emoji and is_emoji(ch):
            out.append(ch)
        elif ch in "\n\t":
            out.append(" ")
        else:
            out.append("?")
    return "".join(out)


def wrap(text: str, max_chars: int, max_lines: int = 3,
         emoji: bool = False) -> list[str]:
    words: list[str] = []
    for line in sanitize(text, emoji=emoji).splitlines():
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


def _ass_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    hours, rest = divmod(seconds, 3600.0)
    minutes, rest = divmod(rest, 60.0)
    return f"{int(hours)}:{int(minutes):02d}:{rest:05.2f}"


def _ass_colour(hex_colour: str, alpha: int = 0) -> str:
    """ASS colours are ``&HAABBGGRR&`` — the reverse of CSS hex."""
    text = (hex_colour or "#FFFFFF").lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    try:
        red, green, blue = (int(text[i:i + 2], 16) for i in (0, 2, 4))
    except (ValueError, IndexError):
        red = green = blue = 255
    return f"&H{alpha:02X}{blue:02X}{green:02X}{red:02X}&"


def _ass_escape(text: str) -> str:
    return (text.replace("\\", "\\\\").replace("{", "(").replace("}", ")")
            .replace("\r", " ").replace("\n", "\\N"))


def build_ass(cues, path: Path, *, font: str = "Arial", font_px: int = 72,
              fill: str = "#FFFFFF", outline: str = "#000000",
              emphasis=(), emphasis_fill: str = HOOK_FILL,
              y: int = 1560, pop: bool = True, margin: int = 64,
              max_width: int = 980) -> bool:
    """Write a self-contained ``.ass`` file for libass.

    This is the post-session caption backend. It gives per-chunk colour,
    karaoke-style word timing and a scale-in pop, and it is one text file to
    restyle instead of dozens of PNGs. ``cues`` is whatever whisper returned;
    the usual popping chunker is applied when ``pop`` is true.
    """
    from . import transcribe  # local import: transcribe only depends on util

    chunks = transcribe.pop_chunks(cues) if pop else list(cues or ())
    if not chunks:
        return False
    wanted = {re.sub(r"[^a-z0-9]", "", word.lower())
              for word in (emphasis or ())}
    normal = _ass_colour(fill)
    hot = _ass_colour(emphasis_fill)
    border = _ass_colour(outline)
    font = (font or "Arial").strip() or "Arial"
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "PlayResX: 1080",
        "PlayResY: 1920",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour,"
        " OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut,"
        " ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow,"
        " Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Pop,{font},{int(font_px)},{normal},{hot},{border},"
        f"&H80000000&,1,0,0,0,100,100,0,0,1,5,1,2,"
        f"{int(margin)},{int(margin)},{int(margin)},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV,"
        " Effect, Text",
    ]
    for cue in chunks:
        text = strip_emoji(cue.text) if has_emoji(cue.text) else cue.text
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        words = text.split()
        # Highlight the whole chunk when one of its words was picked as the
        # stressed word: a gold chunk reads instantly at phone size.
        keys = {re.sub(r"[^a-z0-9]", "", word.lower()) for word in words}
        colour = hot if (wanted and keys & wanted) else ""
        overrides = [r"\an5", f"\\pos(540,{int(y)})"]
        if colour:
            overrides.append(f"\\c{colour}")
        if pop:
            overrides += [r"\fscx84", r"\fscy84", r"\t(0,110,\fscx100\fscy100)"]
        lines.append(
            f"Dialogue: 0,{_ass_time(cue.start)},{_ass_time(cue.end)},Pop,,0,0,0,,"
            f"{{{''.join(overrides)}}}{_ass_escape(text)}"
        )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        return False
    return True


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
