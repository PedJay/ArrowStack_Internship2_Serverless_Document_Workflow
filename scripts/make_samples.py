"""Writes the demo files in samples/ (all simulated data). Re-run any time; output is deterministic."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.sample_files import DELIVERY_NOTE, INVOICE_LINES, STOCK_CSV, minimal_pdf, tiny_png  # noqa: E402

invoice = minimal_pdf(INVOICE_LINES)
SAMPLES = {
    # expected: PROCESSED
    "INV-2026-0042.pdf": invoice,
    "delivery_DN-7781.txt": DELIVERY_NOTE,
    "stock_count_oct.csv": STOCK_CSV,
    "shelf_photo.png": tiny_png(64, 48),
    # expected: DUPLICATE (same bytes as INV-2026-0042.pdf; upload it second)
    "INV-2026-0042 (resent).pdf": invoice,
    # expected: REJECTED
    "edge-cases/invoice_urgent.pdf": b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 120,  # an "exe" posing as a PDF
    "edge-cases/truncated.pdf": invoice[:-40],
    "edge-cases/notes_latin1.txt": "Café order for store 3".encode("latin-1"),
    "edge-cases/photo_really_a_pdf.png": invoice,
}

if __name__ == "__main__":
    for name, data in SAMPLES.items():
        path = ROOT / "samples" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        print(f"{len(data):>6} B  samples/{name}")
