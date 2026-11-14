"""二维码：把一段文字（链接）编成 QR，给 ./install 在终端里画、存 PNG（service/installer.py）。
编码用的是 Project Nayuki 的 QR Code generator library（core/qrcodegen.py，原样 vendored，MIT 许可，版权声明在该文件头部，
来源 https://www.nayuki.io/page/qr-code-generator-library ，PyPI 包 qrcodegen 1.8.0）；这里只有取模块矩阵和写 PNG。
只用标准库。"""
from __future__ import annotations

import struct
import zlib

from covey.core.qrcodegen import QrCode

ECC = {"L": QrCode.Ecc.LOW, "M": QrCode.Ecc.MEDIUM, "Q": QrCode.Ecc.QUARTILE, "H": QrCode.Ecc.HIGH}


def matrix(text, ecc="L"):
    """``text`` as rows of "1" (dark) and "0" (light), no quiet zone; ValueError when it does not fit one code."""
    try:
        code = QrCode.encode_text(text, ECC[ecc])
    except Exception as exc:  # noqa: BLE001 - qrcodegen's DataTooLongError and friends
        raise ValueError(f"cannot encode as a QR code: {exc}") from None
    size = code.get_size()
    return ["".join("1" if code.get_module(x, y) else "0" for x in range(size)) for y in range(size)]


def png(rows, scale=8, border=4):
    """A greyscale PNG of ``rows`` (as ``matrix`` returns them), each module ``scale`` pixels, ``border`` modules of margin."""
    size = len(rows) + 2 * border
    line = []
    for y in range(size):
        cells = ["0"] * size if not border <= y < size - border else (
            ["0"] * border + list(rows[y - border]) + ["0"] * border)
        row = b"".join((b"\x00" if c == "1" else b"\xff") * scale for c in cells)
        line += [b"\x00" + row] * scale

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    side = size * scale
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", side, side, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(line))) + chunk(b"IEND", b""))
