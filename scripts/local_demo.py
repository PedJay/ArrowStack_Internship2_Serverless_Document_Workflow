"""Runs the whole workflow on this PC against moto (fake AWS in memory). No AWS account needed.

The same handler code that runs in Lambda is called with real-shaped events:
API Gateway v2 event -> api_handler; S3 put -> (moto) S3 notification -> SQS -> processor_handler.
Writes evidence/local-demo-<timestamp>.json.

Usage:  .venv\\Scripts\\python scripts\\local_demo.py
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from moto import mock_aws  # noqa: E402

from tests import local_aws  # noqa: E402

local_aws.set_env()


def main() -> int:
    from scenarios import build_cases

    with mock_aws():
        import boto3

        from docflow import api_handler, aws, config, processor_handler
        from tests.conftest import http_event

        config.reset_caches()
        aws.reset_clients()
        ids = local_aws.create_resources()
        s3 = boto3.client("s3", region_name=local_aws.REGION)

        def api(method, path, body=None, query=None, key=local_aws.API_KEY):
            r = api_handler.handler(http_event(method, path, body, key, query), None)
            return r["statusCode"], json.loads(r["body"])

        checks: list[dict] = []

        def check(name: str, ok: bool, detail="") -> None:
            checks.append({"check": name, "result": "PASS" if ok else "FAIL", "detail": detail})

        check("health without key -> 200", api("GET", "/health", key=None)[0] == 200)
        check("upload without key -> 401", api("POST", "/uploads", {}, key=None)[0] == 401)
        status, body = api("POST", "/uploads", {"filename": "tool.exe", "content_type": "application/x-msdownload",
                                                "size_bytes": 0, "uploaded_by": "x"})
        check("invalid upload request -> 422 listing every bad field", status == 422 and len(body["detail"]) == 3,
              [e["field"] for e in body.get("detail", [])])

        run_id = datetime.now(timezone.utc).strftime("%H%M%S")
        timings = []
        for case in build_cases(run_id):
            status, reg = api("POST", "/uploads", {"filename": case.filename, "content_type": case.content_type,
                                                   "size_bytes": len(case.data), "uploaded_by": "ap.clerk@brightmart.example"})
            assert status == 201, reg
            started = time.perf_counter()
            s3.put_object(Bucket=local_aws.BUCKET, Key=reg["upload"]["fields"]["key"], Body=case.data, ContentType=case.content_type)
            result = processor_handler.handler({"Records": local_aws.drain(ids["queue_url"])}, None)
            timings.append((time.perf_counter() - started) * 1000)
            _, doc = api("GET", f"/documents/{reg['document_id']}")
            ok = doc["status"] == case.expected_status and not result["batchItemFailures"]
            if case.expected_class:
                ok = ok and doc.get("doc_class") == case.expected_class
            if case.reason_contains:
                ok = ok and case.reason_contains in (doc.get("reason") or "")
            check(f"{case.name} -> {case.expected_status}", ok,
                  {k: doc.get(k) for k in ("status", "doc_class", "reason", "extracted", "metadata") if doc.get(k)})

        _, processed = api("GET", "/documents", query={"status": "PROCESSED", "limit": "100"})
        check("list PROCESSED -> 4 documents", len(processed["items"]) == 4)
        notes = local_aws.drain(ids["inbox_url"])
        check("one SNS notification per document -> 8", len(notes) == 8)
        listing = s3.list_objects_v2(Bucket=local_aws.BUCKET).get("Contents", [])
        prefixes = sorted({o["Key"].split("/")[0] for o in listing})
        check("incoming/ is empty; files are in processed/ and quarantine/", prefixes == ["processed", "quarantine"], prefixes)

    passed = sum(c["result"] == "PASS" for c in checks)
    report = {
        "kind": "local-demo (moto)",
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "passed": passed,
        "total": len(checks),
        "processing_ms": {"p50": round(statistics.median(timings), 1), "max": round(max(timings), 1),
                          "note": "in-process against moto; not representative of AWS latency"},
        "checks": checks,
    }
    out = ROOT / "evidence" / f"local-demo-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    width = max(len(c["check"]) for c in checks)
    for c in checks:
        print(f"{c['result']}  {c['check']:<{width}}")
    print(f"\n{passed}/{len(checks)} checks passed. Report: {out.relative_to(ROOT)}")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
