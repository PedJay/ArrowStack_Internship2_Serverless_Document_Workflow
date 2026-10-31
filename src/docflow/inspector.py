"""Content inspection: what a file really is, technical metadata, and business fields.

Pure functions over bytes, with no AWS calls, so they are fast to unit test. Everything here is
standard library. No Textract or OCR: those are not free-tier and are on the roadmap.
"""
from __future__ import annotations

import csv
import io
import re
import struct

PDF = "application/pdf"
PNG = "image/png"
JPEG = "image/jpeg"
TEXT = "text/plain"
CSV = "text/csv"

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# Formats we never accept, whatever the file is called
_BLOCKED_SIGNATURES = {
    b"MZ": "application/x-msdownload (Windows executable)",
    b"\x7fELF": "application/x-elf (Linux executable)",
    b"PK\x03\x04": "application/zip (archive or Office document)",
    b"\xca\xfe\xba\xbe": "application/java-vm (Java class)",
    b"#!": "text/x-script (shell script)",
}


class InspectionError(Exception):
    """The file is not acceptable. The message is shown to the uploader."""


def sniff_type(data: bytes) -> str:
    """Detect the real type from the first bytes (magic numbers), ignoring the declared type."""
    head = data[:16]
    if head.startswith(b"%PDF-"):
        return PDF
    if head.startswith(PNG_SIGNATURE):
        return PNG
    if head.startswith(b"\xff\xd8\xff"):
        return JPEG
    for signature, label in _BLOCKED_SIGNATURES.items():
        if head.startswith(signature):
            return label
    # Binary formats are full of NUL bytes; text never has them. The encoding is checked later by
    # extract_metadata, which can tell the uploader exactly what to fix ("re-save as UTF-8")
    if b"\x00" not in data[:4096]:
        return TEXT
    return "application/octet-stream"


def type_matches(declared: str, detected: str) -> bool:
    # CSV has no magic number. It is text that we parse as CSV
    return declared == detected or (declared == CSV and detected == TEXT)


def extract_metadata(data: bytes, content_type: str) -> dict:
    if content_type == PDF:
        return _pdf_metadata(data)
    if content_type == PNG:
        return _png_metadata(data)
    if content_type == JPEG:
        return _jpeg_metadata(data)
    if content_type in (TEXT, CSV):
        return _text_metadata(data, content_type)
    raise InspectionError(f"unsupported content type {content_type}")


def _pdf_metadata(data: bytes) -> dict:
    if b"%%EOF" not in data[-2048:]:
        raise InspectionError("PDF is truncated or damaged (no %%EOF marker)")
    if b"/Encrypt" in data:
        raise InspectionError("password-protected PDFs cannot be processed")
    # "/Type /Page" but not "/Type /Pages" (the page-tree node)
    pages = len(re.findall(rb"/Type\s*/Page(?![a-zA-Z])", data))
    if pages == 0:
        raise InspectionError("PDF has no pages")
    version = data[5:8].decode("ascii", "replace")
    return {"pdf_version": version, "page_count": pages}


def pdf_text(data: bytes) -> str:
    """Best effort: text from *uncompressed* content streams only, e.g. `(Invoice No: 42) Tj`.

    Most real-world PDFs compress their streams, so this often returns "". Classification then
    falls back to the filename.
    """
    parts = re.findall(rb"\(((?:\\.|[^\\)])*)\)\s*Tj", data)
    return " ".join(p.decode("latin-1").replace("\\(", "(").replace("\\)", ")") for p in parts)


def _png_metadata(data: bytes) -> dict:
    if len(data) < 33 or data[12:16] != b"IHDR":
        raise InspectionError("PNG header is damaged")
    width, height, bit_depth = struct.unpack(">IIB", data[16:25])
    if width == 0 or height == 0:
        raise InspectionError("PNG has zero width or height")
    if b"IEND\xaeB`\x82" not in data[-16:]:
        raise InspectionError("PNG is truncated (no IEND chunk)")
    return {"width": width, "height": height, "bit_depth": bit_depth}


# Start-of-frame markers carry the image size. C4 (DHT), C8 (JPG) and CC (DAC) share the range but are not frames
_SOF_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def _jpeg_metadata(data: bytes) -> dict:
    i = 2  # skip SOI (FF D8)
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            raise InspectionError("JPEG structure is damaged")
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker in (0xD9, 0xDA):  # end of image / start of scan before any frame header
            break
        (length,) = struct.unpack(">H", data[i + 2 : i + 4])
        if marker in _SOF_MARKERS and i + 9 <= len(data):
            height, width = struct.unpack(">HH", data[i + 5 : i + 9])
            if width == 0 or height == 0:
                raise InspectionError("JPEG has zero width or height")
            return {"width": width, "height": height}
        i += 2 + length
    raise InspectionError("JPEG has no frame header (damaged or truncated)")


def _text_metadata(data: bytes, content_type: str) -> dict:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise InspectionError(f"text is not valid UTF-8 (byte {exc.start}). Re-save the file as UTF-8") from exc
    if not text.strip():
        raise InspectionError("file contains only whitespace")
    meta = {"encoding": "utf-8", "line_count": len(text.splitlines()), "word_count": len(text.split())}
    if content_type == CSV:
        rows = [r for r in csv.reader(io.StringIO(text)) if any(cell.strip() for cell in r)]
        header, body = rows[0], rows[1:]
        if len(header) < 2:
            raise InspectionError("CSV needs a header row with at least 2 columns")
        meta.update(
            {
                "columns": [h.strip() for h in header][:50],
                "row_count": len(body),
                # rows whose column count differs from the header: kept, but reported
                "inconsistent_rows": sum(1 for r in body if len(r) != len(header)),
            }
        )
    return meta


# ---- business classification -------------------------------------------------------------

_CLASS_KEYWORDS = (
    ("invoice", ("invoice", "tax invoice", "bill no", "inv-")),
    ("delivery_note", ("delivery note", "delivery_note", "dispatch note", "goods received", "dn-")),
    ("purchase_order", ("purchase order", "purchase_order", "po-")),
    ("stock_report", ("stock count", "stock_count", "inventory count", "stock report")),
)
_INVOICE_NO = re.compile(r"invoice\s*(?:no\.?|number|#)\s*[:#]?\s*([A-Z0-9][A-Z0-9/-]{2,24})", re.IGNORECASE)
_TOTAL = re.compile(
    r"(?:grand\s+)?total(?:\s+amount)?(?:\s+due)?\s*[:=]?\s*(?:INR|Rs\.?|₹)?\s*([0-9]{1,3}(?:,[0-9]{2,3})*|[0-9]+)(?:\.([0-9]{2}))?\b",
    re.IGNORECASE,
)


def classify(filename: str, text: str) -> str:
    haystack = f"{filename.lower()} {text[:5000].lower()}"
    for label, keywords in _CLASS_KEYWORDS:
        if any(k in haystack for k in keywords):
            return label
    return "other"


def extract_fields(doc_class: str, text: str) -> dict:
    """Pull out the few fields Accounts Payable keys in by hand today. Amounts in paise (integers)."""
    if doc_class != "invoice" or not text:
        return {}
    fields: dict = {}
    if m := _INVOICE_NO.search(text):
        fields["invoice_number"] = m.group(1).upper()
    totals = list(_TOTAL.finditer(text))
    if totals:
        m = totals[-1]  # the last "total" on an invoice is the grand total, not a line total
        fields["total_amount_minor"] = int(m.group(1).replace(",", "")) * 100 + int(m.group(2) or 0)
        fields["currency"] = "INR"
    return fields
