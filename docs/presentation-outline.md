# Presentation Outline (10–12 min + live demo)

1. **Problem** (1 min): BrightMart's AP team keys invoices in by hand from email attachments. Files go missing,
   re-sent invoices get paid twice, and attachments are opened unchecked.
2. **Goal & success metrics** (1 min): every upload ends in a final state with a reason, byte-identical duplicates
   caught 100%, end-to-end p95 < 30 s, ≤ $5/month, ~$0 idle.
3. **Architecture** (2 min): the overview diagram from architecture.md. Why the file goes **straight to S3** (D2), and
   why there is a **queue** between S3 and Lambda (D3).
4. **Live demo** (3 min):
   * `upload.py samples\INV-2026-0042.pdf` → PROCESSED with invoice number and ₹12,450.50 extracted
   * `upload.py "samples\INV-2026-0042 (resent).pdf"` → **DUPLICATE**, points to the first one
   * `upload.py samples\edge-cases\invoice_urgent.pdf` → **REJECTED: Windows executable**
   * show the emails, then the dashboard
5. **Reliability** (2 min): at-least-once delivery. Show `test_crash_after_copy_is_finished_by_retry` and the DLQ drill
   (poison message → DLQ → alarm → redrive).
6. **Security** (1 min): two least-privilege roles (prefix-scoped), presigned POST policy, magic-byte checks, private
   TLS-only bucket, secret by ARN.
7. **Validation evidence** (1 min): 84 tests / 94%, local demo 14/14, cloud smoke results, 3 defects found by testing.
8. **Backup/recovery & cost** (1 min): S3 versioning + DynamoDB PITR, measured RTO from the drill. Cost table (< $1/month).
9. **Limitations & next steps** (1 min): OCR with Textract, malware scanning, Cognito, outbox for notifications.

## Likely viva questions — short answers

* *Biggest decision?* Direct-to-S3 presigned POST with an exact size condition, plus a queue with a DLQ. S3 enforces the
  rules before any of my code runs, and no document can disappear silently.
* *How do you know it is correct?* Integration tests with real S3→SQS events in moto, an idempotency/crash test,
  local demo 14/14, and the cloud smoke test, which also proves S3 rejects a wrong size or type.
* *10× load?* Raise trigger concurrency and API throttle. Everything else is on-demand (architecture.md § Scaling).
* *Non-technical explanation?* "A digital in-tray: drop a document in, and within seconds it is checked, filed, read
  for the invoice number and amount, and AP gets an email. Copies and dangerous files are caught."
* *Hardest independent problem?* Making processing safe to repeat. Ordering copy → conditional write → delete, and
  letting the hash claim recognise its own document on retry.
