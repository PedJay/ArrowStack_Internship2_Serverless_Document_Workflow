"""End-to-end: API registration -> S3 upload -> real S3->SQS notification (moto) -> processor."""
import json

import boto3
import pytest

from tests import local_aws
from tests.sample_files import DELIVERY_NOTE, INVOICE_LINES, STOCK_CSV, jpeg_header, minimal_pdf, tiny_png

INVOICE_PDF = minimal_pdf(INVOICE_LINES)


def s3():
    return boto3.client("s3", region_name=local_aws.REGION)


def keys(prefix: str) -> list[str]:
    return [o["Key"] for o in s3().list_objects_v2(Bucket=local_aws.BUCKET, Prefix=prefix).get("Contents", [])]


def get_doc(call_api, doc_id):
    status, body, _ = call_api("GET", f"/documents/{doc_id}")
    assert status == 200
    return body


def test_invoice_pdf_is_processed_classified_and_filed(upload, process_queue, call_api, notifications):
    doc_id, incoming = upload("INV-2026-0042.pdf", INVOICE_PDF, "application/pdf")
    result, records = process_queue()
    assert len(records) == 1 and result == {"batchItemFailures": []}

    doc = get_doc(call_api, doc_id)
    assert doc["status"] == "PROCESSED"
    assert doc["doc_class"] == "invoice"
    assert doc["extracted"] == {"invoice_number": "INV-2026-0042", "total_amount_minor": 1245050, "currency": "INR"}
    assert doc["metadata"] == {"pdf_version": "1.4", "page_count": 1}
    assert len(doc["sha256"]) == 64
    assert doc["download_url"].startswith("https://")

    assert keys("incoming/") == []  # moved, not copied
    [stored] = keys("processed/")
    assert stored.endswith(f"/{doc_id}/INV-2026-0042.pdf")
    assert s3().get_object(Bucket=local_aws.BUCKET, Key=stored)["Body"].read() == INVOICE_PDF

    [note] = notifications()
    assert note["status"] == "PROCESSED"
    assert note["subject"] == "[DocFlow] PROCESSED INV-2026-0042.pdf"
    assert note["message"]["extracted"]["invoice_number"] == "INV-2026-0042"


@pytest.mark.parametrize(
    "filename, data, content_type, doc_class, meta_check",
    [
        ("delivery_DN-7781.txt", DELIVERY_NOTE, "text/plain", "delivery_note", {"line_count": 10}),
        ("stock_count_oct.csv", STOCK_CSV, "text/csv", "stock_report", {"row_count": 4, "inconsistent_rows": 1}),
        ("shelf_photo.png", tiny_png(8, 6), "image/png", "other", {"width": 8, "height": 6}),
        ("receipt.jpg", jpeg_header(1024, 768), "image/jpeg", "other", {"width": 1024, "height": 768}),
    ],
)
def test_other_supported_types(upload, process_queue, call_api, filename, data, content_type, doc_class, meta_check):
    doc_id, _ = upload(filename, data, content_type)
    process_queue()
    doc = get_doc(call_api, doc_id)
    assert doc["status"] == "PROCESSED", doc.get("reason")
    assert doc["doc_class"] == doc_class
    assert meta_check.items() <= doc["metadata"].items()


@pytest.mark.parametrize(
    "filename, data, content_type, reason",
    [
        ("invoice_fake.pdf", b"MZ\x90\x00" + b"\x00" * 200, "application/pdf", "Windows executable"),
        ("photo.png", INVOICE_PDF, "image/png", "content is application/pdf but was declared as image/png"),
        ("truncated.pdf", INVOICE_PDF[:-40], "application/pdf", "truncated"),
        ("notes.txt", "café".encode("latin-1"), "text/plain", "not valid UTF-8"),
    ],
)
def test_bad_content_is_quarantined_with_reason(upload, process_queue, call_api, notifications, filename, data, content_type, reason):
    doc_id, _ = upload(filename, data, content_type)
    process_queue()
    doc = get_doc(call_api, doc_id)
    assert doc["status"] == "REJECTED"
    assert reason in doc["reason"]
    assert "download_url" not in doc
    assert keys("incoming/") == [] and keys("processed/") == []
    assert keys("quarantine/") == [f"quarantine/{doc_id}/{filename}"]
    assert notifications()[0]["status"] == "REJECTED"


def test_size_mismatch_is_rejected(upload, process_queue, call_api):
    # The presigned POST blocks this in AWS. This checks the defence-in-depth check in the processor
    doc_id, _ = upload("small.txt", b"only a few bytes", "text/plain", declared_size=5000)
    process_queue()
    doc = get_doc(call_api, doc_id)
    assert doc["status"] == "REJECTED" and "5000 were declared" in doc["reason"]


def test_duplicate_content_is_detected(upload, process_queue, call_api, notifications):
    first, _ = upload("INV-2026-0042.pdf", INVOICE_PDF, "application/pdf")
    process_queue()
    second, _ = upload("INV-2026-0042 (copy).pdf", INVOICE_PDF, "application/pdf")
    process_queue()

    doc = get_doc(call_api, second)
    assert doc["status"] == "DUPLICATE"
    assert doc["duplicate_of"] == first
    assert len(keys("processed/")) == 1  # stored once
    assert [n["status"] for n in notifications()] == ["PROCESSED", "DUPLICATE"]


def test_unregistered_upload_is_quarantined(env, process_queue, notifications):
    # someone with bucket access drops a file in incoming/ without calling POST /uploads
    s3().put_object(Bucket=local_aws.BUCKET, Key="incoming/sneaky.pdf", Body=INVOICE_PDF)
    s3().put_object(Bucket=local_aws.BUCKET, Key="incoming/99999999-9999-9999-9999-999999999999/x.pdf", Body=INVOICE_PDF)
    result, _ = process_queue()
    assert result == {"batchItemFailures": []}
    assert keys("incoming/") == []
    assert sorted(keys("quarantine/unregistered/")) == [
        "quarantine/unregistered/99999999-9999-9999-9999-999999999999/x.pdf",
        "quarantine/unregistered/sneaky.pdf",
    ]
    assert {n["status"] for n in notifications()} == {"REJECTED"}


def test_redelivered_message_is_harmless(upload, call_api, notifications, env):
    from docflow import processor_handler

    doc_id, _ = upload("INV-2026-0042.pdf", INVOICE_PDF, "application/pdf")
    records = local_aws.drain(env["queue_url"])
    processor_handler.handler({"Records": records}, None)
    first = get_doc(call_api, doc_id)
    # SQS delivers the same message again (at-least-once delivery)
    assert processor_handler.handler({"Records": records}, None) == {"batchItemFailures": []}
    assert get_doc(call_api, doc_id)["updated_at"] == first["updated_at"]
    assert len(notifications()) == 1  # no second email


def test_crash_after_copy_is_finished_by_retry(upload, call_api, env, monkeypatch):
    """Simulate the Lambda dying between the S3 copy and the DynamoDB write, then the SQS retry."""
    from docflow import processor_handler, repository

    doc_id, _ = upload("INV-2026-0042.pdf", INVOICE_PDF, "application/pdf")
    records = local_aws.drain(env["queue_url"])
    real_complete = repository.complete

    def crash(*_a, **_k):
        raise ConnectionError("Lambda timed out")

    monkeypatch.setattr(repository, "complete", crash)
    result = processor_handler.handler({"Records": records}, None)
    assert result == {"batchItemFailures": [{"itemIdentifier": records[0]["messageId"]}]}
    assert get_doc(call_api, doc_id)["status"] == "PENDING_UPLOAD"

    monkeypatch.setattr(repository, "complete", real_complete)
    assert processor_handler.handler({"Records": records}, None) == {"batchItemFailures": []}
    doc = get_doc(call_api, doc_id)
    assert doc["status"] == "PROCESSED"  # not DUPLICATE: the hash claim recognises its own doc_id
    assert keys("incoming/") == []


def test_one_bad_message_does_not_fail_the_batch(upload, env):
    from docflow import processor_handler

    upload("INV-2026-0042.pdf", INVOICE_PDF, "application/pdf")
    good = local_aws.drain(env["queue_url"])
    bad = {"messageId": "bad-1", "receiptHandle": "x", "body": "not json"}
    result = processor_handler.handler({"Records": [bad, *good]}, None)
    assert result == {"batchItemFailures": [{"itemIdentifier": "bad-1"}]}


def test_s3_test_event_is_ignored(env):
    from docflow import processor_handler

    test_event = {"messageId": "t", "body": json.dumps({"Service": "Amazon S3", "Event": "s3:TestEvent"})}
    assert processor_handler.handler({"Records": [test_event]}, None) == {"batchItemFailures": []}


def test_url_encoded_keys_in_s3_events_are_decoded(env):
    from docflow import processor_handler

    # S3 events encode a space as "+". Our own keys are sanitised, so only a bypassing upload has one
    s3().put_object(Bucket=local_aws.BUCKET, Key="incoming/scan 1.pdf", Body=INVOICE_PDF)
    local_aws.drain(env["queue_url"])
    event = {"Records": [{"eventName": "ObjectCreated:Put", "eventTime": "2026-10-31T10:00:00.000Z",
                          "s3": {"bucket": {"name": local_aws.BUCKET}, "object": {"key": "incoming/scan+1.pdf"}}}]}
    assert processor_handler.handler({"Records": [{"messageId": "m", "body": json.dumps(event)}]}, None) == {"batchItemFailures": []}
    assert keys("quarantine/") == ["quarantine/unregistered/scan 1.pdf"]


def test_filenames_with_spaces_are_sanitised_before_reaching_s3(upload):
    _, key = upload("INV 2026 0042.pdf", INVOICE_PDF, "application/pdf")
    assert key.endswith("/INV_2026_0042.pdf")


def test_object_deleted_before_processing(upload, call_api, env):
    from docflow import processor_handler

    doc_id, key = upload("INV-2026-0042.pdf", INVOICE_PDF, "application/pdf")
    records = local_aws.drain(env["queue_url"])
    s3().delete_object(Bucket=local_aws.BUCKET, Key=key)
    processor_handler.handler({"Records": records}, None)
    doc = get_doc(call_api, doc_id)
    assert doc["status"] == "REJECTED" and "no longer exists" in doc["reason"]


def test_notification_failure_does_not_reprocess(upload, process_queue, call_api, monkeypatch):
    from botocore.exceptions import ClientError

    from docflow import aws

    class BrokenSns:
        def publish(self, **_):
            raise ClientError({"Error": {"Code": "AuthorizationError", "Message": "denied"}}, "Publish")

    monkeypatch.setattr(aws, "sns", lambda: BrokenSns())
    doc_id, _ = upload("INV-2026-0042.pdf", INVOICE_PDF, "application/pdf")
    result, _ = process_queue()
    assert result == {"batchItemFailures": []}
    assert get_doc(call_api, doc_id)["status"] == "PROCESSED"


def test_emits_cloudwatch_emf_metrics(upload, process_queue, capsys):
    upload("INV-2026-0042.pdf", INVOICE_PDF, "application/pdf")
    process_queue()
    emf = [json.loads(line) for line in capsys.readouterr().out.splitlines() if '"_aws"' in line]
    assert len(emf) == 1
    names = [m["Name"] for m in emf[0]["_aws"]["CloudWatchMetrics"][0]["Metrics"]]
    assert names == ["DocumentsProcessed", "ProcessingDurationMs", "EndToEndLatencyMs"]
    assert emf[0]["_aws"]["CloudWatchMetrics"][0]["Namespace"] == "DocFlow"
    assert emf[0]["DocumentsProcessed"] == 1
