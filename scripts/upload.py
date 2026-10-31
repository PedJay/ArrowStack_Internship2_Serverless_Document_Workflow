"""Upload one file through DocFlow and wait for the result (handy for the live demo).

    .venv\\Scripts\\python scripts\\upload.py --base-url <ApiUrl> --api-key $key samples\\INV-2026-0042.pdf
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import requests

# Not mimetypes.guess_type: on Windows it reads the registry and can call a .csv "application/vnd.ms-excel"
TYPES = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
         ".txt": "text/plain", ".csv": "text/csv"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("file", type=Path)
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--api-key", required=True)
    ap.add_argument("--uploaded-by", default="ap.clerk@brightmart.example")
    ap.add_argument("--content-type", help="override the type guessed from the extension")
    args = ap.parse_args()

    data = args.file.read_bytes()
    content_type = args.content_type or TYPES.get(args.file.suffix.lower(), "application/octet-stream")
    base = args.base_url.rstrip("/")
    headers = {"X-API-Key": args.api_key}

    r = requests.post(f"{base}/uploads", headers=headers, timeout=15, json={
        "filename": args.file.name, "content_type": content_type, "size_bytes": len(data), "uploaded_by": args.uploaded_by})
    if r.status_code != 201:
        print(f"Registration refused ({r.status_code}):\n{json.dumps(r.json(), indent=2)}")
        return 1
    reg = r.json()
    print(f"Registered document {reg['document_id']}. Uploading {len(data)} bytes straight to S3...")

    r = requests.post(reg["upload"]["url"], data=reg["upload"]["fields"], files={"file": (args.file.name, data)}, timeout=60)
    if r.status_code != 204:
        print(f"S3 refused the upload ({r.status_code}): {r.text[:300]}")
        return 1

    started = time.monotonic()
    while time.monotonic() - started < 90:
        doc = requests.get(f"{base}/documents/{reg['document_id']}", headers=headers, timeout=15).json()
        if doc["status"] != "PENDING_UPLOAD":
            if "download_url" in doc:
                doc["download_url"] = "(presigned link, valid 5 minutes)"
            print(f"Finished in {time.monotonic() - started:.1f} s:\n{json.dumps(doc, indent=2)}")
            return 0
        time.sleep(1)
    print("Still PENDING after 90 s - check the intake queue / DLQ (runbook section 'DLQ').")
    return 1


if __name__ == "__main__":
    sys.exit(main())
