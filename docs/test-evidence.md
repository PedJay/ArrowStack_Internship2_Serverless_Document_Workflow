# Test & Validation Evidence

## 1. Automated tests (local, moto-mocked AWS)

Command: `.venv\Scripts\python -m pytest -v --cov=docflow`. Full output: [`evidence/pytest-report.txt`](../evidence/pytest-report.txt)

**Result (2026-10-31): 84 passed, 0 failed. Statement coverage 94%.**

The processor tests are true integration tests in moto: a real S3 `put_object` triggers moto's **S3 → SQS
notification**, and the queued message is fed to the Lambda handler exactly as the SQS trigger would.

| Area | Tests | What is proven |
|---|---|---|
| Inspector (unit) | 30 | Magic-byte detection for 10 inputs incl. EXE/ELF/ZIP/script. PDF pages/version/text. Truncated, encrypted and page-less PDFs refused. PNG/JPEG size and damage. UTF-8/BOM/whitespace/CSV header rules. Ragged-row count. Classification (6 cases). Invoice number + **grand** total in paise |
| Validation (unit) | 21 | Path traversal, Windows paths, Unicode lookalikes and long names sanitised. 11 invalid-field cases. All errors reported together. `True` is not accepted as a size |
| API | 12 | Health without key. 401 on every protected route. **Key read from Secrets Manager, rotation picked up after cache reset**. Presigned POST policy pins exact size and type. 422 with field details, 400 bad JSON/base64, 404/405. Pagination newest-first. Bad tokens refused. 500 hides internals |
| Processor (integration) | 21 | Invoice processed, filed, fields extracted, email sent. TXT/CSV/PNG/JPEG processed. 4 kinds of bad content quarantined with reasons. Size mismatch. **Duplicate detection**. Unregistered uploads quarantined. **Redelivery is harmless (no second email)**. **Crash between copy and DB write is finished by the retry**. One bad message doesn't fail the batch. S3 test event ignored. URL-encoded keys. Object deleted before processing. SNS failure doesn't reprocess. EMF metric shape |

## 2. Infrastructure validation

| Check | Result |
|---|---|
| `cfn-lint infra/template.yaml` ([evidence/cfn-lint.txt](../evidence/cfn-lint.txt)) | ✅ 0 errors. 2 × W3002, which is expected: `Code: ../src` is a local path that `aws cloudformation package` replaces |
| Dashboard body is valid JSON after `!Sub` | ✅ 6 widgets (checked with a script during the build) |
| PowerShell syntax of `deploy.ps1` / `teardown.ps1` | ✅ 0 parse errors (PowerShell AST parser) |

## 3. Local end-to-end demo

`.venv\Scripts\python scripts\local_demo.py` runs the same scenarios as the cloud smoke test, in moto.
Report: `evidence/local-demo-<timestamp>.json`

**14 / 14 checks passed:** health, 401 without key, 422 lists every bad field, invoice PDF → PROCESSED with fields,
delivery note → PROCESSED, CSV with a ragged row → PROCESSED, PNG → PROCESSED, EXE renamed to `.pdf` → REJECTED,
truncated PDF → REJECTED, Latin-1 text → REJECTED, same invoice again → DUPLICATE, listing shows 4 processed,
8 notifications for 8 documents, `incoming/` empty afterwards.

### Defects found by testing (and fixed)

| # | Found by | Defect | Fix |
|---|---|---|---|
| 1 | processor integration tests | moto sends S3 `eventTime` without a timezone. The latency metric raised `TypeError` **after** the document was filed, so the message was retried | Naive timestamps are treated as UTC. Metrics errors are now caught and logged and can never fail a message |
| 2 | local demo | Latin-1 text was refused as "application/octet-stream", which doesn't tell the uploader what to do | NUL-free content counts as text, and strict UTF-8 decoding then gives the specific reason (D18) |
| 4 | cloud smoke test (latency) | End-to-end p50 was 21.8 s although processing took < 1 s: the 2 s SQS batching window made the poller hold messages ~20 s at low volume | Batching window set to 0 (D21). p50 now 1.2 s |
| 3 | code review | `MAX_UPLOAD_BYTES` was built by string concatenation in CloudFormation (`10` → `10048576`) | Parameter is now in bytes (D16) |

## 4. Cloud validation (2026-10-31, stack `docflow`, ap-south-1)

**Smoke test: 26 / 26 PASS** (`evidence/smoke-20261031T151522Z.json`, before D21, and `evidence/smoke-20261031T151908Z.json`, after D21).

| Metric | Before (batching window 2 s) | After (window 0) | Target N4 |
|---|---|---|---|
| End-to-end p50 (client-observed) | 21.8 s | **1.2 s** | < 30 s ✅ |
| End-to-end max | 22.3 s | **2.3 s** | |
| Processing per document (EMF) | 93–610 ms | same | p95 < 2 s ✅ |

| # | Evidence to capture | Result / file |
|---|---|---|
| C1 | `deploy.ps1` output table (stack outputs) | screenshot |
| C2 | `smoke_test.py`: **all checks PASS**, incl. S3 refusing wrong size/type and public GET | ✅ 26/26, `evidence/smoke-20261031T151908Z.json` + screenshot |
| C3 | `upload.py` of `samples\INV-2026-0042.pdf` showing extracted invoice number and total | screenshot |
| C4 | AP email for PROCESSED + REJECTED, and an alarm email | screenshots |
| C5 | CloudWatch dashboard after C2 (outcomes, latency p50/p95, queue) | screenshot |
| C6 | Logs Insights: one `doc_id` traced from API log → processor log | screenshot |
| C7 | IAM: both role policies (prefix-scoped S3, single table/topic/queue/secret) | screenshots |
| C8 | S3: Block Public Access, versioning, encryption, lifecycle rules, event notification → SQS | screenshots |
| C9 | DLQ drill: poison message → 3 receives → DLQ → alarm email → redrive | screenshots |
| C10 | Restore drill (runbook): S3 version restore + DynamoDB PITR restore. **Measured RTO = ___ min** | runbook + screenshots |
| C11 | DynamoDB items: a PROCESSED, a REJECTED and a DUPLICATE record, plus a `SHA256#` claim item | screenshot |
| C12 | Budget visible in Billing → Budgets | screenshot |
| C13 | Measured end-to-end latency p50 / max from the smoke report vs the N4 target (< 30 s) | ✅ p50 1.2 s / max 2.3 s (table above) |
