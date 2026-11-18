"""The conclusion band's orb as a PNG (docs/ui.md 光球): a glowing ball in a state's colour on a transparent ground,
drawn here so that no image file lives in the repository. feishu/orb_images.py uploads it once and keeps its key.

``png(key)`` is the same bytes for the same key on every machine (no timestamp, fixed compression), so a changed
drawing is a changed hash and is uploaded again. Standard library only."""
from __future__ import annotations

import hashlib
import math
import struct
import zlib

SIZE = 96  # Pixels a side: shown at 40px, sharp on a 2x screen.
# The ball's colour of each state of core/look.py ORB_ORDER, as the web page's (core/look.py STATES web).
COLORS = {"waiting": (0xF5, 0xA5, 0x24), "stopped": (0xF2, 0x55, 0x5A), "stuck": (0xF5, 0xA5, 0x24),
          "result": (0x3D, 0xD6, 0x8C), "running": (0x4C, 0x9A, 0xFF)}


def _pixel(distance, color):
    """RGBA at ``distance`` from the centre (0 centre, 1 the edge): a white core, the colour, a halo fading out."""
    r, g, b = color
    if distance <= 0.12:
        return 255, 255, 255, 255
    if distance <= 0.40:  # White into the colour.
        t = ((distance - 0.12) / 0.28) ** 0.8
        return (round(255 + (r - 255) * t), round(255 + (g - 255) * t), round(255 + (b - 255) * t), 255)
    if distance <= 1.0:  # The halo: the colour, its alpha falling off smoothly to nothing at the edge.
        t = (distance - 0.40) / 0.60
        return r, g, b, round(255 * 0.85 * (1 - t) ** 1.7)
    return r, g, b, 0


def png(key) -> bytes:
    color = COLORS[key]
    half = SIZE / 2
    rows = []
    for y in range(SIZE):
        row = bytearray(b"\x00")  # Filter: none.
        for x in range(SIZE):
            distance = math.hypot(x + 0.5 - half, y + 0.5 - half) / half
            row += bytes(_pixel(distance, color))
        rows.append(bytes(row))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    header = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0)  # 8-bit RGBA.
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
            + chunk(b"IEND", b""))


def digest(key) -> str:
    return hashlib.sha256(png(key)).hexdigest()[:16]
