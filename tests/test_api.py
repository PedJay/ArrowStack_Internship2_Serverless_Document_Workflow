import base64
import json

import boto3

from tests import local_aws
from tests.conftest import http_event

UPLOAD = {"filename": "invoice INV-1.pdf", "content_type": "application/pdf", "size_bytes": 2048, "uploaded_by": "ap@brightmart.example"}


def test_health_needs_no_key(call_api):
    status, body, headers = call_api("GET", "/health", api_key=None)
    assert (status, body) == (200, {"status": "ok"})
    assert headers["x-request-id"] == "req-test"


def test_protected_routes_need_the_key(call_api):
    for method, path in (("POST", "/uploads"), ("GET", "/documents"), ("GET", "/documents/00000000-0000-0000-0000-000000000000")):
        assert call_api(method, path, UPLOAD, api_key=None)[0] == 401
        assert call_api(method, path, UPLOAD, api_key="wrong-key")[0] == 401


def test_api_key_comes_from_secrets_manager(env, call_api):
    assert call_api("POST", "/uploads", UPLOAD)[0] == 201  # loads and caches the key
    boto3.client("secretsmanager", region_name=local_aws.REGION).put_secret_value(
        SecretId=env["secret_arn"], SecretString="rotated-key"
    )
    # still cached: the old key keeps working until the cache expires, so rotation causes no outage
    assert call_api("POST", "/uploads", UPLOAD)[0] == 201
    from docflow import config

    config.reset_caches()
    assert call_api("POST", "/uploads", UPLOAD)[0] == 401
    assert call_api("POST", "/uploads", UPLOAD, api_key="rotated-key")[0] == 201


def test_create_upload_registers_document_and_returns_presigned_post(call_api):
    status, body, _ = call_api("POST", "/uploads", UPLOAD)
    assert status == 201
    doc_id = body["document_id"]
    fields = body["upload"]["fields"]
    assert fields["key"] == f"incoming/{doc_id}/invoice_INV-1.pdf"
    assert fields["Content-Type"] == "application/pdf"
    policy = json.loads(base64.b64decode(fields["policy"]))
    # S3 will refuse any other size or content type
    assert ["content-length-range", 2048, 2048] in policy["conditions"]
    assert {"Content-Type": "application/pdf"} in policy["conditions"]
    assert body["upload"]["url"].startswith("https://")

    status, doc, _ = call_api("GET", f"/documents/{doc_id}")
    assert status == 200
    assert doc["status"] == "PENDING_UPLOAD"
    assert doc["filename"] == "invoice_INV-1.pdf"
    assert doc["size_bytes"] == 2048
    assert "expires_at" not in doc and "source_ip" not in doc  # internal fields stay internal


def test_upload_validation_errors_are_422_with_details(call_api):
    status, body, _ = call_api("POST", "/uploads", {**UPLOAD, "content_type": "application/x-msdownload", "size_bytes": 0})
    assert status == 422
    assert {e["field"] for e in body["detail"]} == {"content_type", "size_bytes"}


def test_malformed_json_is_400(call_api):
    assert call_api("POST", "/uploads", "{not json")[0] == 400


def test_base64_body_is_decoded(env):
    from docflow import api_handler

    event = http_event("POST", "/uploads", base64.b64encode(json.dumps(UPLOAD).encode()).decode())
    event["isBase64Encoded"] = True
    assert api_handler.handler(event, None)["statusCode"] == 201
    event["body"] = "%%%"
    assert api_handler.handler(event, None)["statusCode"] == 400


def test_unknown_route_and_wrong_method(call_api):
    assert call_api("GET", "/nope")[0] == 404
    assert call_api("GET", "/documents/not-a-uuid")[0] == 404
    assert call_api("DELETE", "/documents")[0] == 405
    assert call_api("POST", "/health")[0] == 405


def test_unknown_document_is_404(call_api):
    assert call_api("GET", "/documents/11111111-2222-3333-4444-555555555555")[0] == 404


def test_list_documents_by_status_with_pagination(call_api):
    ids = [call_api("POST", "/uploads", UPLOAD)[1]["document_id"] for _ in range(3)]
    status, page1, _ = call_api("GET", "/documents", query={"status": "pending_upload", "limit": "2"})
    assert status == 200
    assert len(page1["items"]) == 2 and page1["next_token"]
    _, page2, _ = call_api("GET", "/documents", query={"status": "PENDING_UPLOAD", "limit": "2", "next_token": page1["next_token"]})
    seen = [d["document_id"] for d in page1["items"] + page2["items"]]
    assert sorted(seen) == sorted(ids)
    # newest first
    assert page1["items"][0]["created_at"] >= page1["items"][1]["created_at"]


def test_list_documents_rejects_bad_parameters(call_api):
    assert call_api("GET", "/documents", query={"status": "LOST"})[0] == 422
    assert call_api("GET", "/documents", query={"limit": "0"})[0] == 422
    assert call_api("GET", "/documents", query={"limit": "abc"})[0] == 422
    assert call_api("GET", "/documents", query={"next_token": "garbage!"})[0] == 422
    other = base64.urlsafe_b64encode(json.dumps({"status": "REJECTED"}).encode()).decode()
    assert call_api("GET", "/documents", query={"status": "PROCESSED", "next_token": other})[0] == 422


def test_internal_errors_are_hidden(call_api, monkeypatch):
    from docflow import repository

    def boom(*_):
        raise RuntimeError("database password is hunter2")

    monkeypatch.setattr(repository, "get", boom)
    status, body, _ = call_api("GET", "/documents/11111111-2222-3333-4444-555555555555")
    assert status == 500
    assert "hunter2" not in json.dumps(body)
    assert body["request_id"] == "req-test"
