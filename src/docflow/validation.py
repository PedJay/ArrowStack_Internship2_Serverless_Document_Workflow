"""Validation of upload requests. Runs in the API function, before any S3 URL is issued."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath

from docflow.config import ALLOWED_TYPES

MAX_FILENAME_CHARS = 100
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[A-Za-z]{2,}$")
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


class RequestValidationError(Exception):
    def __init__(self, errors: list[dict]):
        super().__init__("; ".join(f"{e['field']}: {e['error']}" for e in errors))
        self.errors = errors


@dataclass(frozen=True)
class UploadRequest:
    filename: str  # sanitised, safe to use in an S3 key
    original_filename: str
    content_type: str
    size_bytes: int
    uploaded_by: str


def sanitize_filename(name: str) -> str:
    """Return a safe filename, or "" if nothing usable is left.

    Drops any directory part (also Windows-style), so "../../etc/passwd" can't escape
    the document's prefix. Normalises Unicode lookalikes and replaces anything outside
    A-Z a-z 0-9 . _ - with "_".
    """
    name = unicodedata.normalize("NFKC", name).replace("\\", "/")
    name = PurePosixPath(name).name
    name = _UNSAFE_CHARS.sub("_", name).strip("._-")
    if len(name) > MAX_FILENAME_CHARS:
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 10:
            name = stem[: MAX_FILENAME_CHARS - len(ext) - 1] + "." + ext
        else:
            name = name[:MAX_FILENAME_CHARS]
    return name


def validate_upload_request(body: object, max_upload_bytes: int) -> UploadRequest:
    errors: list[dict] = []
    if not isinstance(body, dict):
        raise RequestValidationError([{"field": "body", "error": "must be a JSON object"}])

    raw_name = body.get("filename")
    filename = ""
    if not isinstance(raw_name, str) or not raw_name.strip():
        errors.append({"field": "filename", "error": "required"})
    elif len(raw_name) > 255:
        errors.append({"field": "filename", "error": "longer than 255 characters"})
    else:
        filename = sanitize_filename(raw_name)
        if not filename or "." not in filename:
            errors.append({"field": "filename", "error": "must contain a name and an extension"})

    content_type = body.get("content_type")
    if not isinstance(content_type, str) or content_type.lower() not in ALLOWED_TYPES:
        errors.append({"field": "content_type", "error": f"must be one of {sorted(ALLOWED_TYPES)}"})
    else:
        content_type = content_type.lower()
        if filename and not filename.lower().endswith(ALLOWED_TYPES[content_type]):
            errors.append(
                {"field": "filename", "error": f"extension does not match {content_type} (expected {', '.join(ALLOWED_TYPES[content_type])})"}
            )

    size = body.get("size_bytes")
    # bool is a subclass of int in Python, so reject it explicitly
    if not isinstance(size, int) or isinstance(size, bool):
        errors.append({"field": "size_bytes", "error": "must be an integer"})
    elif size < 1:
        errors.append({"field": "size_bytes", "error": "empty files are not accepted"})
    elif size > max_upload_bytes:
        errors.append({"field": "size_bytes", "error": f"larger than the {max_upload_bytes} byte limit"})

    uploaded_by = body.get("uploaded_by")
    if not isinstance(uploaded_by, str) or not EMAIL_RE.match(uploaded_by):
        errors.append({"field": "uploaded_by", "error": "must be an email address"})

    if errors:
        raise RequestValidationError(errors)
    return UploadRequest(
        filename=filename,
        original_filename=raw_name,
        content_type=content_type,
        size_bytes=size,
        uploaded_by=uploaded_by.lower(),
    )
