"""Builders for small but structurally valid test documents (simulated data only).

Shared by the tests and scripts/make_samples.py, so the sample files in samples/ are exactly
what the test suite exercises.
"""
from __future__ import annotations

import struct
import zlib


def minimal_pdf(lines: list[str], pages: int = 1) -> bytes:
    """A real, viewable PDF with uncompressed text (so DocFlow can read it)."""

    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    text = "BT /F1 12 Tf 72 720 Td 16 TL " + " ".join(f"({esc(line)}) Tj T*" for line in lines) + " ET"
    stream = text.encode("latin-1")
    page_ids = [4 + i for i in range(pages)]
    font_id, content_id = 4 + pages, 5 + pages
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: f"<< /Type /Pages /Kids [{' '.join(f'{p} 0 R' for p in page_ids)}] /Count {pages} >>".encode(),
        3: b"<< /Producer (DocFlow sample generator) >>",
        font_id: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        content_id: b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    }
    for p in page_ids:
        objects[p] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents {content_id} 0 R "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>"
        ).encode()

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    for num in sorted(objects):
        offsets[num] = len(out)
        out += f"{num} 0 obj\n".encode() + objects[num] + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for num in sorted(objects):
        out += f"{offsets[num]:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R /Info 3 0 R >>\nstartxref\n{xref_at}\n%%EOF\n".encode()
    return bytes(out)


def tiny_png(width: int = 4, height: int = 3, rgb=(46, 125, 50)) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def jpeg_header(width: int = 640, height: int = 480) -> bytes:
    """Just enough JPEG structure for the parser (SOI, APP0, SOF0, EOI). Not a viewable image."""
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, height, width, 3) + b"\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


INVOICE_LINES = [
    "BrightMart Supplies Pvt Ltd (simulated)",
    "TAX INVOICE",
    "Invoice No: INV-2026-0042",
    "Supplier: Konkan Cotton Mills (fictional)",
    "Line 1: 40 x Black T-Shirt (M) @ 249.00 = 9,960.00",
    "Line 2: 10 x Cap @ 249.05 = 2,490.50",
    "Total: INR 12,450.50",
]

DELIVERY_NOTE = """DELIVERY NOTE DN-7781
Supplier: Konkan Cotton Mills (fictional)
Delivered to: BrightMart Store 3, Panaji
Date: 2026-10-29

SKU            QTY
TSHIRT-BLK-M   40
CAP-RED        10

Received by: store desk (simulated)
""".encode("utf-8")

STOCK_CSV = """sku,name,counted_qty,location
TSHIRT-BLK-M,Black T-Shirt (M),37,A1
CAP-RED,Red Cap,10,B4
MUG-WHT,White Mug,0,C2
SOCKS-GRY,Grey Socks,12
""".encode("utf-8")
