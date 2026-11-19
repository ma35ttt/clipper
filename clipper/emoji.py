"""Real colour emoji, straight out of the system font.

Apple Color Emoji is a *bitmap* font: every glyph is a PNG stored in its
``sbix`` table. FreeType, which ImageMagick uses, only surfaces a grey alpha
mask for those glyphs, so drawing one with a gold fill turns the iPhone's
flame into a flat silhouette. Reading the PNG out of the font keeps the emoji
looking like the phone that produced it.

Pure standard library: ``mmap`` and ``struct``. A font without an ``sbix``
table (Windows' Segoe UI Emoji, Noto's CBDT files) simply returns ``None`` and
the caller falls back to the silhouette path.
"""

from __future__ import annotations

import mmap
import struct
from functools import lru_cache
from pathlib import Path

# Not glyphs on their own: joiners and variation selectors sit between the
# pictographs they modify.
_SKIP = {0x200D, 0xFE0E, 0xFE0F, 0x20E3}


class _Face:
    """A lazily-read font file that can hand back an emoji's PNG bytes."""

    def __init__(self, path: str):
        self._handle = open(path, "rb")
        self._buf = mmap.mmap(self._handle.fileno(), 0, access=mmap.ACCESS_READ)
        self._cmap = 0
        self._strike = 0
        self._strike_end = 0
        self._offsets = 0
        self._glyphs = 0
        if not self._parse():
            raise ValueError("no sbix/cmap")

    # -- header --------------------------------------------------------
    def _u16(self, offset: int) -> int:
        return struct.unpack_from(">H", self._buf, offset)[0]

    def _u32(self, offset: int) -> int:
        return struct.unpack_from(">I", self._buf, offset)[0]

    def _tables(self, face: int) -> dict[str, tuple[int, int]]:
        tables: dict[str, tuple[int, int]] = {}
        for index in range(self._u16(face + 4)):
            record = face + 12 + index * 16
            tag = self._buf[record:record + 4].decode("latin-1")
            tables[tag] = (self._u32(record + 8), self._u32(record + 12))
        return tables

    def _parse(self) -> bool:
        faces = [0]
        if self._buf[:4] == b"ttcf":
            count = self._u32(8)
            faces = [self._u32(12 + index * 4) for index in range(count)]
        for face in faces:
            try:
                tables = self._tables(face)
            except struct.error:
                continue
            if "cmap" not in tables or "sbix" not in tables:
                continue
            self._glyphs = self._u16(tables["maxp"][0] + 4) if "maxp" in tables else 0
            offset = self._format12(tables["cmap"][0])
            self._cmap = tables["cmap"][0] + offset if offset else 0
            self._strike, self._offsets = self._largest_strike(tables["sbix"][0])
            if self._cmap and self._strike:
                return True
        return False

    def _format12(self, cmap: int) -> int:
        """Byte offset of the format-12 subtable, which handles astral planes."""
        for index in range(self._u16(cmap + 2)):
            record = cmap + 4 + index * 8
            offset = self._u32(record + 4)
            if self._u16(cmap + offset) == 12:
                return offset
        return 0

    def _largest_strike(self, sbix: int) -> tuple[int, int]:
        """The sharpest bitmap strike, and the table of per-glyph offsets."""
        best, area = 0, -1
        for index in range(self._u32(sbix + 4)):
            strike = sbix + self._u32(sbix + 8 + index * 4)
            ppem = self._u16(strike)
            if ppem > area:
                best, area = strike, ppem
        if not best:
            return 0, 0
        return best, best + 4

    # -- lookup --------------------------------------------------------
    def glyph(self, code: int) -> int:
        if not self._cmap or not self._glyphs:
            return 0
        base = self._cmap
        low, high = 0, self._u32(base + 12) - 1
        while low <= high:
            middle = (low + high) // 2
            group = base + 16 + middle * 12
            start, end, first = struct.unpack_from(">III", self._buf, group)
            if code < start:
                high = middle - 1
            elif code > end:
                low = middle + 1
            else:
                return first + code - start
        return 0

    def png(self, code: int) -> bytes | None:
        glyph = self.glyph(code)
        if not glyph or not self._offsets:
            return None
        start = self._u32(self._offsets + glyph * 4)
        end = self._u32(self._offsets + (glyph + 1) * 4)
        if end <= start:
            return None
        data = self._strike + start
        if self._buf[data + 4:data + 8] != b"png ":
            return None
        return bytes(self._buf[data + 8:self._strike + end])


@lru_cache(maxsize=4)
def _face(path: str) -> _Face | None:
    try:
        return _Face(path)
    except (OSError, ValueError, struct.error):
        return None


@lru_cache(maxsize=512)
def glyph_png(font: str, char: str) -> bytes | None:
    """The colour PNG for one emoji character, or None when unavailable."""
    code = ord(char)
    if code in _SKIP or 0x1F3FB <= code <= 0x1F3FF:  # joiners, skin tones
        return None
    face = _face(font)
    if face is None:
        return None
    try:
        return face.png(code)
    except (OSError, struct.error):
        return None


def digest(font: str, text: str) -> list[tuple[str, bytes]]:
    """Split a run into drawable (char, png) pairs, skipping joiners."""
    out: list[tuple[str, bytes]] = []
    for char in text:
        if ord(char) in _SKIP:
            continue
        data = glyph_png(font, char)
        if data:
            out.append((char, data))
    return out