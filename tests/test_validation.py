import pytest

from docflow.validation import RequestValidationError, sanitize_filename, validate_upload_request

LIMIT = 10 * 1024 * 1024
GOOD = {"filename": "invoice.pdf", "content_type": "application/pdf", "size_bytes": 1234, "uploaded_by": "AP@BrightMart.example"}


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("invoice.pdf", "invoice.pdf"),
        ("../../etc/passwd.txt", "passwd.txt"),
        ("C:\\Users\\clerk\\Desktop\\bill 7.pdf", "bill_7.pdf"),
        ("Rechnung März <final>.pdf", "Rechnung_M_rz_final_.pdf"),
        ("ｉｎｖｏｉｃｅ.pdf", "invoice.pdf"),  # full-width lookalikes are normalised
        ("...", ""),
        ("a" * 300 + ".pdf", "a" * 96 + ".pdf"),
    ],
)
def test_sanitize_filename(raw, expected):
    assert sanitize_filename(raw) == expected


def test_valid_request_is_normalised():
    req = validate_upload_request({**GOOD, "content_type": "Application/PDF"}, LIMIT)
    assert (req.filename, req.content_type, req.size_bytes, req.uploaded_by) == (
        "invoice.pdf", "application/pdf", 1234, "ap@brightmart.example",
    )


@pytest.mark.parametrize(
    "patch, field",
    [
        ({"filename": ""}, "filename"),
        ({"filename": "x" * 256 + ".pdf"}, "filename"),
        ({"filename": "noextension"}, "filename"),
        ({"filename": "invoice.exe"}, "filename"),  # extension must match the declared type
        ({"content_type": "application/x-msdownload"}, "content_type"),
        ({"content_type": None}, "content_type"),
        ({"size_bytes": 0}, "size_bytes"),
        ({"size_bytes": LIMIT + 1}, "size_bytes"),
        ({"size_bytes": "1234"}, "size_bytes"),
        ({"size_bytes": True}, "size_bytes"),
        ({"uploaded_by": "not-an-email"}, "uploaded_by"),
    ],
)
def test_invalid_fields(patch, field):
    with pytest.raises(RequestValidationError) as err:
        validate_upload_request({**GOOD, **patch}, LIMIT)
    assert field in [e["field"] for e in err.value.errors]


def test_all_errors_reported_together():
    with pytest.raises(RequestValidationError) as err:
        validate_upload_request({}, LIMIT)
    assert {e["field"] for e in err.value.errors} == {"filename", "content_type", "size_bytes", "uploaded_by"}


def test_body_must_be_object():
    with pytest.raises(RequestValidationError):
        validate_upload_request(["not", "an", "object"], LIMIT)
