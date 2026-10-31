"""The validation scenarios, shared by local_demo.py (moto) and smoke_test.py (real AWS).

Content embeds a run id, so a second run never collides with the first run's SHA-256
(which would otherwise turn every document into a DUPLICATE).
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.sample_files import DELIVERY_NOTE, INVOICE_LINES, STOCK_CSV, minimal_pdf, tiny_png  # noqa: E402


@dataclass
class Case:
    name: str
    filename: str
    data: bytes
    content_type: str
    expected_status: str
    expected_class: str | None = None
    reason_contains: str | None = None


def build_cases(run_id: str) -> list[Case]:
    invoice = minimal_pdf([*INVOICE_LINES, f"Reference: run {run_id}"])
    return [
        Case("invoice PDF -> fields extracted", f"INV-2026-0042-{run_id}.pdf", invoice, "application/pdf", "PROCESSED", "invoice"),
        Case("delivery note (text)", f"delivery_DN-7781-{run_id}.txt", DELIVERY_NOTE + f"run {run_id}\n".encode(), "text/plain",
             "PROCESSED", "delivery_note"),
        Case("stock count (CSV, 1 ragged row)", f"stock_count_{run_id}.csv", STOCK_CSV + f"RUN-{run_id},marker,0,Z9\n".encode(),
             "text/csv", "PROCESSED", "stock_report"),
        Case("shelf photo (PNG)", f"shelf_photo_{run_id}.png", tiny_png(8, 6, (int(run_id[-2:]) % 256, 120, 40)), "image/png",
             "PROCESSED", "other"),
        Case("executable renamed to .pdf", f"invoice_urgent_{run_id}.pdf", b"MZ\x90\x00" + run_id.encode() + b"\x00" * 200,
             "application/pdf", "REJECTED", reason_contains="executable"),
        Case("truncated PDF", f"truncated_{run_id}.pdf", invoice[:-40], "application/pdf", "REJECTED", reason_contains="truncated"),
        Case("non-UTF-8 text", f"notes_{run_id}.txt", f"Caf\xe9 order {run_id}".encode("latin-1"), "text/plain", "REJECTED",
             reason_contains="UTF-8"),
        # must run after the first case has finished
        Case("same invoice uploaded again", f"INV-2026-0042-{run_id} (copy).pdf", invoice, "application/pdf", "DUPLICATE",
             reason_contains="same content"),
    ]
