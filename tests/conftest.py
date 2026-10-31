import json
import sys
from pathlib import Path

import pytest
from moto import mock_aws

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from tests import local_aws  # noqa: E402

local_aws.set_env()


@pytest.fixture()
def env():
    """A fresh fake AWS account with the bucket, queue, table, topic and secret."""
    local_aws.set_env()
    with mock_aws():
        from docflow import aws, config

        config.reset_caches()
        aws.reset_clients()
        ids = local_aws.create_resources()
        yield ids
        config.reset_caches()
        aws.reset_clients()


def http_event(method: str, path: str, body=None, api_key: str | None = local_aws.API_KEY, query=None) -> dict:
    """An API Gateway HTTP API (payload v2.0) event, the same shape Lambda receives."""
    headers = {"content-type": "application/json"}
    if api_key is not None:
        headers["x-api-key"] = api_key
    return {
        "version": "2.0",
        "rawPath": path,
        "headers": headers,
        "queryStringParameters": query,
        "requestContext": {"requestId": "req-test", "http": {"method": method, "path": path, "sourceIp": "203.0.113.7"}},
        "body": body if isinstance(body, str) or body is None else json.dumps(body),
        "isBase64Encoded": False,
    }


@pytest.fixture()
def call_api(env):
    from docflow import api_handler

    def _call(method, path, body=None, api_key=local_aws.API_KEY, query=None):
        resp = api_handler.handler(http_event(method, path, body, api_key, query), None)
        return resp["statusCode"], json.loads(resp["body"]), resp["headers"]

    return _call


@pytest.fixture()
def upload(env, call_api):
    """Register a document through the API, then put the bytes where the presigned POST would."""
    import boto3

    s3 = boto3.client("s3", region_name=local_aws.REGION)

    def _upload(filename: str, data: bytes, content_type: str, declared_size: int | None = None):
        status, body, _ = call_api(
            "POST", "/uploads",
            {"filename": filename, "content_type": content_type, "size_bytes": declared_size or len(data),
             "uploaded_by": "ap.clerk@brightmart.example"},
        )
        assert status == 201, body
        key = body["upload"]["fields"]["key"]
        s3.put_object(Bucket=local_aws.BUCKET, Key=key, Body=data, ContentType=content_type)
        return body["document_id"], key

    return _upload


@pytest.fixture()
def process_queue(env):
    """Feed every S3 event waiting in the intake queue to the processor, like the SQS trigger does."""
    from docflow import processor_handler

    def _run():
        records = local_aws.drain(env["queue_url"])
        return processor_handler.handler({"Records": records}, None), records

    return _run


@pytest.fixture()
def notifications(env):
    def _read():
        return [
            {"subject": json.loads(r["body"]).get("Subject"), "message": json.loads(json.loads(r["body"])["Message"]),
             "status": json.loads(r["body"])["MessageAttributes"]["status"]["Value"]}
            for r in local_aws.drain(env["inbox_url"])
        ]

    return _read
