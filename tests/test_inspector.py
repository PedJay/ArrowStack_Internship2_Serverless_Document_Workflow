import pytest

from docflow import inspector
from docflow.inspector import InspectionError
from tests.sample_files import INVOICE_LINES, STOCK_CSV, jpeg_header, minimal_pdf, tiny_png


@pytest.mark.parametrize(
    "data, expected",
    [
        (minimal_pdf(["hi"]), "application/pdf"),
        (tiny_png(), "image/png"),
        (jpeg_header(), "image/jpeg"),
        (b"hello, world\n", "text/plain"),
        ("नमस्ते ₹ 500\n".encode(), "text/plain"),
        (b"MZ\x90\x00\x03" + b"\x00" * 20, "application/x-msdownload (Windows executable)"),
        (b"PK\x03\x04" + b"\x00" * 20, "application/zip (archive or Office document)"),
        (b"\x7fELF\x02\x01", "application/x-elf (Linux executable)"),
        (b"#!/bin/sh\nrm -rf /\n", "text/x-script (shell script)"),
        (b"\x00\x01\x02binary", "application/octet-stream"),
    ],
)
def test_sniff_type(data, expected):
    assert inspector.sniff_type(data) == expected


def test_non_utf8_text_is_still_text_so_the_encoding_error_is_specific():
    data = "Café order 7".encode("latin-1")
    assert inspector.sniff_type(data) == "text/plain"
    with pytest.raises(InspectionError, match=r"not valid UTF-8 \(byte 3\)"):
        inspector.extract_metadata(data, "text/plain")


def test_type_matches_allows_csv_as_text_only():
    assert inspector.type_matches("text/csv", "text/plain")
    assert inspector.type_matches("application/pdf", "application/pdf")
    assert not inspector.type_matches("application/pdf", "text/plain")
    assert not inspector.type_matches("text/plain", "text/csv")


def test_pdf_metadata_and_text():
    pdf = minimal_pdf(INVOICE_LINES, pages=3)
    assert inspector.extract_metadata(pdf, "application/pdf") == {"pdf_version": "1.4", "page_count": 3}
    assert "Invoice No: INV-2026-0042" in inspector.pdf_text(pdf)


@pytest.mark.parametrize(
    "data, message",
    [
        (minimal_pdf(["x"])[:-30], "truncated"),
        (minimal_pdf(["x"]).replace(b"/Producer", b"/Encrypt "), "password-protected"),
        (b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\n%%EOF", "no pages"),
    ],
)
def test_bad_pdfs_rejected(data, message):
    with pytest.raises(InspectionError, match=message):
        inspector.extract_metadata(data, "application/pdf")


def test_png_metadata_and_damage():
    assert inspector.extract_metadata(tiny_png(4, 3), "image/png") == {"width": 4, "height": 3, "bit_depth": 8}
    with pytest.raises(InspectionError, match="truncated"):
        inspector.extract_metadata(tiny_png()[:-12], "image/png")
    with pytest.raises(InspectionError, match="damaged"):
        inspector.extract_metadata(b"\x89PNG\r\n\x1a\n" + b"\x00" * 30, "image/png")


def test_jpeg_metadata_and_damage():
    assert inspector.extract_metadata(jpeg_header(640, 480), "image/jpeg") == {"width": 640, "height": 480}
    with pytest.raises(InspectionError, match="no frame header"):
        inspector.extract_metadata(b"\xff\xd8\xff\xd9", "image/jpeg")
    with pytest.raises(InspectionError, match="damaged"):
        inspector.extract_metadata(b"\xff\xd8\x00\x00\x00\x00", "image/jpeg")


def test_text_and_csv_metadata():
    meta = inspector.extract_metadata("﻿line one\nline two words\n".encode(), "text/plain")
    assert meta == {"encoding": "utf-8", "line_count": 2, "word_count": 5}
    csv_meta = inspector.extract_metadata(STOCK_CSV, "text/csv")
    assert csv_meta["columns"] == ["sku", "name", "counted_qty", "location"]
    assert csv_meta["row_count"] == 4
    assert csv_meta["inconsistent_rows"] == 1  # SOCKS-GRY is missing its location


@pytest.mark.parametrize(
    "data, content_type, message",
    [
        (b"caf\xe9 latin-1", "text/plain", "not valid UTF-8"),
        (b"   \n\t  ", "text/plain", "only whitespace"),
        (b"just_one_column\nvalue\n", "text/csv", "at least 2 columns"),
        (b"anything", "application/zip", "unsupported"),
    ],
)
def test_bad_text_rejected(data, content_type, message):
    with pytest.raises(InspectionError, match=message):
        inspector.extract_metadata(data, content_type)


@pytest.mark.parametrize(
    "filename, text, expected",
    [
        ("scan_0001.pdf", "TAX INVOICE ...", "invoice"),
        ("INV-2026-0042.pdf", "", "invoice"),
        ("note.txt", "Delivery Note DN-1", "delivery_note"),
        ("PO-55.pdf", "", "purchase_order"),
        ("stock_count_oct.csv", "sku,qty", "stock_report"),
        ("holiday_photo.png", "", "other"),
    ],
)
def test_classify(filename, text, expected):
    assert inspector.classify(filename, text) == expected


def test_extract_invoice_fields_uses_grand_total_in_paise():
    text = "\n".join(INVOICE_LINES)
    assert inspector.extract_fields("invoice", text) == {
        "invoice_number": "INV-2026-0042",
        "total_amount_minor": 1245050,
        "currency": "INR",
    }
    assert inspector.extract_fields("invoice", "Invoice # ab-77\nGrand total: Rs. 900") == {
        "invoice_number": "AB-77", "total_amount_minor": 90000, "currency": "INR",
    }
    assert inspector.extract_fields("delivery_note", text) == {}
    assert inspector.extract_fields("invoice", "") == {}
