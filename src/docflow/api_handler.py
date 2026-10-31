"""HTTP API Lambda (API Gateway HTTP API, payload format 2.0).

Routes
  GET  /health                       liveness, no auth
  POST /uploads                      register a document and get a presigned S3 POST
  GET  /documents?status=&limit=&next_token=
  GET  /documents/{id}               status, metadata and (if processed) a download link
Everything except /health needs the X-API-Key header.

The file itself never passes through Lambda or API Gateway. The client uploads it
straight to S3 with the presigned POST, which pins the exact key, content type and
size. This avoids the 6 MB Lambda payload limit and Lambda time spent on bytes.
"""
from __future__ import annotations

import base64
import binascii
import hmac
import json
import logging
import re
import time
import uuid

from docflow import aws, repository
from docflow.config import get_api_key, get_settings
from docflow.observability import configure_logging, log
from docflow.validation import RequestValidationError, validate_upload_request

logger = logging.getLogger("docflow.api")
configure_logging()

_DOC_PATH = re.compile(r"^/documents/([0-9a-f-]{36})$")
_PUBLIC_FIELDS = (
    "doc_id", "filename", "content_type", "size_bytes", "uploaded_by", "status", "created_at", "updated_at",
    "doc_class", "metadata", "extracted", "sha256", "reason", "duplicate_of", "processing_ms",
)


class HttpError(Exception):
    def __init__(self, status: int, detail):
        super().__init__(str(detail))
        self.status = status
        self.detail = detail


def handler(event: dict, context) -> dict:
    started = time.perf_counter()
    http = event.get("requestContext", {}).get("http", {})
    method = http.get("method", "GET")
    path = event.get("rawPath", "/")
    request_id = event.get("requestContext", {}).get("requestId") or str(uuid.uuid4())
    try:
        status, body = _route(method, path, event)
    except HttpError as exc:
        status, body = exc.status, {"detail": exc.detail}
    except Exception:  # never leak internals to the caller
        logger.exception("unhandled error", extra={"extra_fields": {"request_id": request_id}})
        status, body = 500, {"detail": "internal error", "request_id": request_id}

    log(
        logger, logging.INFO, "request",
        request_id=request_id, method=method, path=path, status=status,
        duration_ms=round((time.perf_counter() - started) * 1000, 1),
        source_ip=http.get("sourceIp"),
    )
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json", "x-request-id": request_id},
        "body": json.dumps(body, default=str),
    }


def _route(method: str, path: str, event: dict) -> tuple[int, dict]:
    if path == "/health":
        _allow(method, "GET")
        return 200, {"status": "ok"}

    if path == "/uploads":
        _allow(method, "POST")
        _authenticate(event)
        return _create_upload(event)
    if path == "/documents":
        _allow(method, "GET")
        _authenticate(event)
        return _list_documents(event.get("queryStringParameters") or {})
    if m := _DOC_PATH.match(path):
        _allow(method, "GET")
        _authenticate(event)
        return _get_document(m.group(1))
    raise HttpError(404, "not found")


def _allow(method: str, expected: str) -> None:
    if method != expected:
        raise HttpError(405, f"method {method} not allowed")


def _authenticate(event: dict) -> None:
    supplied = (event.get("headers") or {}).get("x-api-key", "")
    # constant-time comparison, so response timing can't be used to guess the key
    if not supplied or not hmac.compare_digest(supplied.encode(), get_api_key().encode()):
        raise HttpError(401, "missing or invalid X-API-Key")


def _json_body(event: dict):
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        try:
            raw = base64.b64decode(raw).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise HttpError(400, "body is not valid base64 UTF-8") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HttpError(400, "body must be valid JSON") from exc


def _create_upload(event: dict) -> tuple[int, dict]:
    settings = get_settings()
    try:
        req = validate_upload_request(_json_body(event), settings.max_upload_bytes)
    except RequestValidationError as exc:
        raise HttpError(422, exc.errors) from exc

    doc_id = str(uuid.uuid4())
    key = f"incoming/{doc_id}/{req.filename}"
    repository.create_pending(
        {
            "doc_id": doc_id,
            "filename": req.filename,
            "original_filename": req.original_filename,
            "content_type": req.content_type,
            "size_bytes": req.size_bytes,
            "uploaded_by": req.uploaded_by,
            "incoming_key": key,
            "source_ip": event.get("requestContext", {}).get("http", {}).get("sourceIp"),
        }
    )
    # S3 itself enforces these conditions: wrong type, other size or other key -> 403 from S3
    post = aws.s3().generate_presigned_post(
        Bucket=settings.bucket,
        Key=key,
        Fields={"Content-Type": req.content_type},
        Conditions=[
            {"Content-Type": req.content_type},
            ["content-length-range", req.size_bytes, req.size_bytes],
        ],
        ExpiresIn=settings.upload_url_ttl_seconds,
    )
    log(logger, logging.INFO, "upload registered", doc_id=doc_id, filename=req.filename,
        content_type=req.content_type, size_bytes=req.size_bytes)
    return 201, {
        "document_id": doc_id,
        "status": repository.PENDING,
        "upload": {"url": post["url"], "fields": post["fields"], "expires_in": settings.upload_url_ttl_seconds},
        "status_url": f"/documents/{doc_id}",
    }


def _public_view(item: dict) -> dict:
    view = {k: item[k] for k in _PUBLIC_FIELDS if k in item}
    view["document_id"] = view.pop("doc_id")
    return repository.to_json(view)


def _get_document(doc_id: str) -> tuple[int, dict]:
    item = repository.get(doc_id)
    if not item:
        raise HttpError(404, "document not found")
    body = _public_view(item)
    if item["status"] == repository.PROCESSED and item.get("storage_key"):
        settings = get_settings()
        body["download_url"] = aws.s3().generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.bucket, "Key": item["storage_key"]},
            ExpiresIn=settings.download_url_ttl_seconds,
        )
    return 200, body


def _list_documents(params: dict) -> tuple[int, dict]:
    status = params.get("status", repository.PROCESSED).upper()
    if status not in (repository.PENDING, *repository.FINAL_STATUSES):
        raise HttpError(422, [{"field": "status", "error": f"must be one of {[repository.PENDING, *repository.FINAL_STATUSES]}"}])
    try:
        limit = int(params.get("limit", "20"))
    except ValueError:
        limit = 0
    if not 1 <= limit <= 100:
        raise HttpError(422, [{"field": "limit", "error": "must be an integer from 1 to 100"}])

    start_key = None
    if token := params.get("next_token"):
        try:
            start_key = json.loads(base64.urlsafe_b64decode(token.encode()))
            if not isinstance(start_key, dict) or start_key.get("status") != status:
                raise ValueError
        except (ValueError, binascii.Error) as exc:
            raise HttpError(422, [{"field": "next_token", "error": "invalid or belongs to another status"}]) from exc

    items, last_key = repository.list_by_status(status, limit, start_key)
    next_token = base64.urlsafe_b64encode(json.dumps(last_key).encode()).decode() if last_key else None
    return 200, {"items": [_public_view(i) for i in items], "next_token": next_token}
