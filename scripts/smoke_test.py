"""End-to-end validation against the DEPLOYED stack. Produces evidence/smoke-<timestamp>.json.

Usage (after deploy.ps1):
    $key = aws secretsmanager get-secret-value --secret-id docflow/api-key --region ap-south-1 --query SecretString --output text
    .venv\\Scripts\\python scripts\\smoke_test.py --base-url <ApiUrl> --api-key $key
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scenarios import build_cases  # noqa: E402

FINAL = {"PROCESSED", "REJECTED", "DUPLICATE"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--api-key", required=True)
    ap.add_argument("--timeout", type=int, default=90, help="seconds to wait for each document")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[1] / "evidence"))
    args = ap.parse_args()

    base = args.base_url.rstrip("/")
    s = requests.Session()
    s.headers["X-API-Key"] = args.api_key
    results: list[dict] = []

    def check(name: str, ok: bool, detail=None, ms: float | None = None) -> bool:
        results.append({"check": name, "result": "PASS" if ok else "FAIL", "detail": detail, "ms": ms})
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if not ok and detail else ""))
        return ok

    def register(filename: str, content_type: str, size: int) -> dict:
        r = s.post(f"{base}/uploads", json={"filename": filename, "content_type": content_type, "size_bytes": size,
                                            "uploaded_by": "smoke.test@brightmart.example"}, timeout=15)
        r.raise_for_status()
        return r.json()

    def s3_post(reg: dict, data: bytes, filename: str, override: dict | None = None) -> requests.Response:
        fields = {**reg["upload"]["fields"], **(override or {})}
        # plain requests.post (no API key header) - S3 must accept it on the signature alone
        return requests.post(reg["upload"]["url"], data=fields, files={"file": (filename, data)}, timeout=30)

    def wait_final(doc_id: str) -> tuple[dict, float]:
        started = time.monotonic()
        while time.monotonic() - started < args.timeout:
            doc = s.get(f"{base}/documents/{doc_id}", timeout=15).json()
            if doc.get("status") in FINAL:
                return doc, (time.monotonic() - started) * 1000
            time.sleep(1)
        return doc, (time.monotonic() - started) * 1000

    # --- API surface
    r = requests.get(f"{base}/health", timeout=15)
    check("GET /health -> 200 (no key needed)", r.status_code == 200, r.status_code)
    r = requests.post(f"{base}/uploads", json={}, timeout=15)
    check("POST /uploads without key -> 401", r.status_code == 401, r.status_code)
    r = s.post(f"{base}/uploads", json={"filename": "tool.exe", "content_type": "application/x-msdownload", "size_bytes": 0,
                                        "uploaded_by": "x"}, timeout=15)
    check("invalid upload request -> 422", r.status_code == 422, r.text[:200])
    r = s.post(f"{base}/uploads", data="{not json", headers={"content-type": "application/json"}, timeout=15)
    check("malformed JSON -> 400", r.status_code == 400, r.status_code)

    # --- S3 enforces the presigned POST conditions
    reg = register("size_check.txt", "text/plain", 20)
    r = s3_post(reg, b"x" * 25, "size_check.txt")
    check("S3 refuses a body larger than declared", r.status_code in (400, 403), f"{r.status_code} {r.text[:120]}")
    reg = register("type_check.pdf", "application/pdf", 10)
    r = s3_post(reg, b"0123456789", "type_check.pdf", {"Content-Type": "text/plain"})
    check("S3 refuses a different Content-Type", r.status_code == 403, f"{r.status_code} {r.text[:120]}")
    bucket_url = reg["upload"]["url"].rstrip("/")
    r = requests.get(f"{bucket_url}/{reg['upload']['fields']['key']}", timeout=15)
    check("anonymous GET on the bucket -> 403 (public access blocked)", r.status_code in (403, 404), r.status_code)

    # --- the workflow
    run_id = datetime.now(timezone.utc).strftime("%H%M%S")
    latencies: list[float] = []
    docs: dict[str, dict] = {}
    for case in build_cases(run_id):
        reg = register(case.filename, case.content_type, len(case.data))
        r = s3_post(reg, case.data, case.filename)
        if not check(f"upload '{case.name}' to S3 -> 204", r.status_code == 204, f"{r.status_code} {r.text[:200]}"):
            continue
        doc, ms = wait_final(reg["document_id"])
        latencies.append(ms)
        docs[case.name] = doc
        ok = doc.get("status") == case.expected_status
        if case.expected_class:
            ok = ok and doc.get("doc_class") == case.expected_class
        if case.reason_contains:
            ok = ok and case.reason_contains in (doc.get("reason") or "")
        check(f"{case.name} -> {case.expected_status}", ok,
              {k: doc.get(k) for k in ("status", "doc_class", "reason", "extracted") if doc.get(k)}, round(ms))

    invoice_case = build_cases(run_id)[0]
    invoice = docs.get(invoice_case.name, {})
    if invoice.get("download_url"):
        r = requests.get(invoice["download_url"], timeout=30)
        check("presigned download returns the exact bytes (SHA-256)",
              r.status_code == 200 and hashlib.sha256(r.content).hexdigest() == invoice.get("sha256"), r.status_code)
    check("invoice number + total extracted",
          invoice.get("extracted", {}).get("invoice_number") == "INV-2026-0042"
          and invoice.get("extracted", {}).get("total_amount_minor") == 1245050, invoice.get("extracted"))

    listed = s.get(f"{base}/documents", params={"status": "PROCESSED", "limit": 100}, timeout=15).json()
    check("GET /documents?status=PROCESSED includes the invoice",
          any(d["document_id"] == invoice.get("document_id") for d in listed.get("items", [])))

    passed = sum(r["result"] == "PASS" for r in results)
    report = {
        "kind": "cloud smoke test",
        "base_url": base,
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "passed": passed,
        "total": len(results),
        "observed_latency_ms": {
            "note": "client-observed: S3 upload finished -> status final via API polling (1 s poll interval)",
            "p50": round(statistics.median(latencies)) if latencies else None,
            "max": round(max(latencies)) if latencies else None,
        },
        "checks": results,
    }
    out = Path(args.out) / f"smoke-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\n{passed}/{len(results)} checks passed. Report: {out}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
