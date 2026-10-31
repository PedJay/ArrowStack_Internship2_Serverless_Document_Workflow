# Requirements — DocFlow: Serverless Supplier-Document Intake

## 1. Stakeholders & problem

| Role | Need |
|---|---|
| **Accounts Payable (AP) team** (primary) | Every supplier invoice in one place with its invoice number and total. Never pay the same invoice twice |
| Store receiving staff (uploaders) | Upload a delivery note or invoice photo in seconds and know straight away if it was refused, and why |
| Finance controller | Proof of what was received and when (audit trail). Documents kept and recoverable |
| IT / cloud engineer (me) | No servers to patch, safe against malicious files, observable, cheap when idle |

**Problem statement.** BrightMart (a fictional retailer; Project 1's business) receives supplier invoices, delivery notes
and stock counts by email and messaging apps. Staff save them to shared folders and AP types the invoice number and
total into the books by hand. Files go missing. A supplier who re-sends an invoice can get paid twice. Attachments are
opened without any check, so a renamed `.exe` is a real risk.

**Decision enabled.** AP pays an invoice only when its DocFlow record is `PROCESSED` (not `DUPLICATE`), using the
extracted invoice number and total as a pre-filled check.
**Measurable benefit.** Duplicate re-sends are caught automatically (target: 100% of byte-identical re-sends). A document
is available to AP within seconds of upload instead of "whenever someone files it". Every refused file gets a written reason.

## 2. Scope

**In scope:** upload registration through an authenticated HTTP API; direct-to-S3 upload with size and type locked by the
presigned POST; asynchronous processing via a queue; type verification by content; metadata extraction for PDF/PNG/JPEG/
TXT/CSV; duplicate detection; document classification; invoice field extraction from text; quarantine with reasons; email
notifications; status and listing API; monitoring, alarms, backups, budget; one-command deploy and teardown.

**Out of scope (on the roadmap):** OCR of scanned images/compressed PDFs (Amazon Textract), malware scanning (GuardDuty
Malware Protection for S3), per-user identities (Cognito), a web UI, integration with an accounting system, multi-region.

## 3. Inputs, outputs, constraints

| | |
|---|---|
| Inputs | `POST /uploads` JSON (filename, content type, exact size, uploader email), then the file bytes, sent straight to S3 |
| Outputs | Document record (status, reason, class, technical metadata, extracted fields, SHA-256), presigned download link, SNS email per outcome, CloudWatch metrics |
| Accepted files | PDF, PNG, JPEG, UTF-8 TXT, UTF-8 CSV, 1 byte – 10 MiB (configurable) |
| Constraints | AWS free tier / ≤ $5 per month, simulated data only (no real supplier or personal data), deployable by a mentor from the README |

## 4. Functional requirements

| ID | Requirement |
|---|---|
| F1 | A client registers an upload and receives a short-lived (5 min) presigned S3 POST bound to one key, one content type and the **exact declared size** |
| F2 | The API rejects bad registrations with **all** field errors at once (422): unknown type, extension not matching the type, empty/oversized, missing uploader |
| F3 | Filenames are sanitised (path components stripped, Unicode normalised, unsafe characters replaced) before they reach S3 |
| F4 | Each uploaded file is processed automatically. Its **real type is detected from its bytes** and must match the declared type. Executables, archives and scripts are always refused |
| F5 | Damaged files are refused with a specific reason: truncated/encrypted/page-less PDF, damaged PNG/JPEG, non-UTF-8 text, CSV without a header |
| F6 | Technical metadata is stored: SHA-256, pages/PDF version, image dimensions, line/word counts, CSV columns/row count/ragged rows |
| F7 | Documents are classified (invoice, delivery note, purchase order, stock report, other). For invoices, the invoice number and grand total (integer paise) are extracted when the text is readable |
| F8 | A byte-identical re-upload is marked `DUPLICATE` with a link to the original. It is not stored twice and not processed twice |
| F9 | Outcomes: accepted files go to `processed/`, refused files to `quarantine/` (kept 30 days), `incoming/` is always left empty |
| F10 | An object that appears in `incoming/` without a registration (API bypassed) is quarantined and reported |
| F11 | An email is sent per outcome. Which outcomes are emailed is configurable (e.g. exceptions only) |
| F12 | `GET /documents/{id}` returns status and metadata, plus a 5-minute download link when processed. `GET /documents?status=` lists newest first with pagination |
| F13 | All routes except `/health` require `X-API-Key` |

## 5. Non-functional requirements & success metrics

| ID | Requirement | Target / measure |
|---|---|---|
| N1 | Security | No credentials in code, template or env vars. One least-privilege role per function. Bucket private, TLS-only, encrypted. Content-based type check |
| N2 | Reliability | No lost documents: each upload ends in a final status, or in the DLQ with an alarm. Processing is idempotent under at-least-once delivery |
| N3 | Observability | JSON logs, EMF metrics per outcome and latency, 5 alarms, 1 dashboard |
| N4 | Performance | End-to-end (upload done → status final) p95 **< 30 s** in AWS. Processing time per document p95 < 2 s |
| N5 | Recoverability | Documents: S3 versioning (30 days of old versions). Records: DynamoDB PITR (35 days). RPO ≤ 5 min, RTO ≤ 30 min |
| N6 | Cost | ≤ $5/month with a budget alert. ~$0 when idle (no always-on compute) |
| N7 | Quality | ≥ 90% automated test coverage. All local demo checks and all cloud smoke checks pass |
| N8 | Reproducibility | Whole environment created and deleted by one script each |

## 6. Acceptance criteria → evidence

| Criterion | Evidence |
|---|---|
| Core requirements done and mapped | README "How each requirement is covered" table |
| Reproducible by a mentor | README + [RUN-GUIDE.md](../RUN-GUIDE.md), `deploy.ps1` / `teardown.ps1`, `local_demo.py` needs no AWS |
| Quality checks documented | [test-evidence.md](test-evidence.md), `evidence/` |
| Known issues disclosed | [architecture.md § Limitations](architecture.md#limitations--roadmap) |
| Decisions explained | [decision-log.md](decision-log.md) |
