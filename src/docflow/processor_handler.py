"""Processor Lambda: SQS (fed by S3 ObjectCreated events on incoming/) -> inspect -> file -> notify.

For each uploaded object:
  1. look up its registration in DynamoDB (objects nobody registered are quarantined)
  2. check size, real type (magic bytes) vs declared type, and that the content is readable
  3. detect duplicates by SHA-256 (atomic claim in DynamoDB)
  4. copy to processed/ or quarantine/, write the final record, delete from incoming/
  5. publish a notification to SNS and emit metrics

Safe to run twice for the same object, because SQS and S3 events are at-least-once:
  * the final DynamoDB write is conditional on status = PENDING_UPLOAD
  * the copy happens *before* that write and the delete *after*, so a crash at any point
    leaves the object somewhere and a retry finishes the job
Failures are reported per message (ReportBatchItemFailures), so one bad message does not
make the whole batch retry. After 3 failed receives a message goes to the dead-letter queue.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone
from urllib.parse import unquote_plus

from botocore.exceptions import ClientError

from docflow import aws, inspector, repository
from docflow.config import get_settings
from docflow.observability import configure_logging, emit_metrics, log

logger = logging.getLogger("docflow.processor")
configure_logging()

_INCOMING_KEY = re.compile(r"^incoming/([0-9a-f-]{36})/([^/]+)$")
_METRIC_BY_STATUS = {
    repository.PROCESSED: "DocumentsProcessed",
    repository.REJECTED: "DocumentsRejected",
    repository.DUPLICATE: "DocumentsDuplicate",
}


class Rejection(Exception):
    """The document fails a business rule. It is quarantined, not retried."""


def handler(event: dict, context) -> dict:
    failures = []
    for record in event.get("Records", []):
        try:
            _process_sqs_message(record)
        except Exception:
            logger.exception("message failed, will be retried", extra={"extra_fields": {"message_id": record.get("messageId")}})
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}


def _process_sqs_message(record: dict) -> None:
    body = json.loads(record["body"])
    if body.get("Event") == "s3:TestEvent":  # S3 sends this once when the notification is configured
        log(logger, logging.INFO, "ignored S3 test event")
        return
    for s3_record in body.get("Records", []):
        if not s3_record.get("eventName", "").startswith("ObjectCreated"):
            continue
        process_object(
            bucket=s3_record["s3"]["bucket"]["name"],
            # keys in S3 events are URL-encoded ("my file.pdf" arrives as "my+file.pdf")
            key=unquote_plus(s3_record["s3"]["object"]["key"]),
            event_time=s3_record.get("eventTime"),
        )


def process_object(bucket: str, key: str, event_time: str | None = None) -> str:
    """Process one uploaded object. Returns the final status (or 'SKIPPED')."""
    started = time.perf_counter()
    m = _INCOMING_KEY.match(key)
    doc = repository.get(m.group(1)) if m else None
    if not doc or doc.get("incoming_key") != key:
        return _quarantine_unregistered(bucket, key)

    doc_id = doc["doc_id"]
    if doc["status"] != repository.PENDING:
        # Redelivery after a successful run: the copy and DB write are done, only the delete may be missing
        _delete_quietly(bucket, key)
        log(logger, logging.INFO, "already processed, skipped", doc_id=doc_id, status=doc["status"])
        return "SKIPPED"

    object_exists = True
    try:
        status, fields = _inspect(bucket, key, doc)
    except _ObjectGone:
        object_exists = False
        status, fields = repository.REJECTED, {"reason": "uploaded object no longer exists"}
    except Rejection as exc:
        status, fields = repository.REJECTED, {"reason": str(exc)}

    if status == repository.PROCESSED:
        now = datetime.now(timezone.utc)
        fields["storage_key"] = f"processed/{now:%Y/%m}/{doc_id}/{doc['filename']}"
        _copy(bucket, key, fields["storage_key"])
    elif status == repository.REJECTED and object_exists:
        fields["quarantine_key"] = f"quarantine/{doc_id}/{doc['filename']}"
        _copy(bucket, key, fields["quarantine_key"])
    # DUPLICATE: the original is already stored, so the copy is simply dropped

    fields["processing_ms"] = int((time.perf_counter() - started) * 1000)
    fields["processed_at"] = repository.now_iso()
    won = repository.complete(doc_id, status, fields)
    _delete_quietly(bucket, key)
    if not won:
        log(logger, logging.INFO, "another invocation finished first", doc_id=doc_id)
        return "SKIPPED"

    final = {**doc, **fields, "status": status}
    _notify(final)
    try:
        _metrics(final, event_time)
    except Exception:  # the document is already filed; a metrics bug must not trigger a retry
        logger.exception("metrics failed", extra={"extra_fields": {"doc_id": doc_id}})
    log(logger, logging.INFO, "document processed", doc_id=doc_id, status=status, reason=fields.get("reason"),
        doc_class=fields.get("doc_class"), processing_ms=fields["processing_ms"])
    return status


class _ObjectGone(Exception):
    pass


def _inspect(bucket: str, key: str, doc: dict) -> tuple[str, dict]:
    settings = get_settings()
    s3 = aws.s3()
    try:
        head = s3.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            raise _ObjectGone() from exc
        raise

    size = head["ContentLength"]
    # The presigned POST already enforces the size. These checks guard against anything that bypassed it
    if size == 0:
        raise Rejection("file is empty")
    if size > settings.max_upload_bytes:
        raise Rejection(f"file is {size} bytes, over the {settings.max_upload_bytes} byte limit")
    if size != int(doc["size_bytes"]):
        raise Rejection(f"file is {size} bytes but {doc['size_bytes']} were declared")

    data = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    sha256 = hashlib.sha256(data).hexdigest()
    detected = inspector.sniff_type(data)
    fields: dict = {"sha256": sha256, "detected_type": detected}
    if not inspector.type_matches(doc["content_type"], detected):
        raise Rejection(f"content is {detected} but was declared as {doc['content_type']}")
    try:
        fields["metadata"] = inspector.extract_metadata(data, doc["content_type"])
    except inspector.InspectionError as exc:
        raise Rejection(str(exc)) from exc

    owner = repository.claim_hash(sha256, doc["doc_id"])
    if owner != doc["doc_id"]:
        return repository.DUPLICATE, {**fields, "duplicate_of": owner, "reason": f"same content as document {owner}"}

    if doc["content_type"] == inspector.PDF:
        text = inspector.pdf_text(data)
    elif doc["content_type"] in (inspector.TEXT, inspector.CSV):
        text = data.decode("utf-8-sig")
    else:
        text = ""
    fields["doc_class"] = inspector.classify(doc["filename"], text)
    fields["extracted"] = inspector.extract_fields(fields["doc_class"], text)
    return repository.PROCESSED, fields


def _copy(bucket: str, source_key: str, dest_key: str) -> None:
    aws.s3().copy_object(
        Bucket=bucket,
        Key=dest_key,
        CopySource={"Bucket": bucket, "Key": source_key},
        MetadataDirective="COPY",
        ServerSideEncryption="AES256",
    )


def _delete_quietly(bucket: str, key: str) -> None:
    try:
        aws.s3().delete_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        log(logger, logging.WARNING, "could not delete incoming object", key=key, error=str(exc))


def _quarantine_unregistered(bucket: str, key: str) -> str:
    """An object in incoming/ that no POST /uploads call registered: someone bypassed the API."""
    dest = f"quarantine/unregistered/{key.removeprefix('incoming/')}"
    try:
        _copy(bucket, key, dest)
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in ("404", "NoSuchKey", "NotFound"):
            raise
        return "SKIPPED"  # already moved by an earlier delivery
    _delete_quietly(bucket, key)
    log(logger, logging.WARNING, "unregistered object quarantined", key=key, quarantine_key=dest)
    emit_metrics("processor", {"DocumentsRejected": (1, "Count")}, reason="unregistered", key=key)
    _publish(
        subject="[DocFlow] REJECTED unregistered upload",
        message={"status": repository.REJECTED, "reason": "object was not registered via POST /uploads", "key": key,
                 "quarantine_key": dest},
        status=repository.REJECTED,
    )
    return repository.REJECTED


def _notify(doc: dict) -> None:
    message = {
        "document_id": doc["doc_id"],
        "status": doc["status"],
        "filename": doc["filename"],
        "uploaded_by": doc["uploaded_by"],
        "doc_class": doc.get("doc_class"),
        "extracted": repository.to_json(doc.get("extracted") or {}),
        "reason": doc.get("reason"),
        "duplicate_of": doc.get("duplicate_of"),
    }
    _publish(subject=f"[DocFlow] {doc['status']} {doc['filename']}"[:100], message=message, status=doc["status"])


def _publish(subject: str, message: dict, status: str) -> None:
    topic = get_settings().topic_arn
    if not topic:
        return
    try:
        aws.sns().publish(
            TopicArn=topic,
            Subject=subject,
            Message=json.dumps({k: v for k, v in message.items() if v not in (None, {}, "")}, indent=2, default=str),
            # subscribers filter on this, e.g. the finance mailbox only wants REJECTED and DUPLICATE
            MessageAttributes={"status": {"DataType": "String", "StringValue": status}},
        )
    except ClientError as exc:
        # The document is already filed correctly. A lost email must not cause reprocessing
        log(logger, logging.ERROR, "notification failed", error=str(exc), subject=subject)


def _metrics(doc: dict, event_time: str | None) -> None:
    metrics = {_METRIC_BY_STATUS[doc["status"]]: (1, "Count"), "ProcessingDurationMs": (doc["processing_ms"], "Milliseconds")}
    if event_time:
        try:
            uploaded = datetime.fromisoformat(event_time.replace("Z", "+00:00"))
            if uploaded.tzinfo is None:  # S3 always sends UTC, but not every emulator adds the "Z"
                uploaded = uploaded.replace(tzinfo=timezone.utc)
            end_to_end = (datetime.now(timezone.utc) - uploaded).total_seconds() * 1000
            metrics["EndToEndLatencyMs"] = (round(end_to_end), "Milliseconds")
        except ValueError:
            pass
    emit_metrics("processor", metrics, doc_id=doc["doc_id"], status=doc["status"], doc_class=doc.get("doc_class"))
