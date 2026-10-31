# Operations Runbook — DocFlow

All commands are PowerShell. Set these first:

```powershell
$Stack = "docflow"; $Region = "ap-south-1"
function Out-Value($k) { aws cloudformation describe-stacks --stack-name $Stack --region $Region --query "Stacks[0].Outputs[?OutputKey=='$k'].OutputValue" --output text }
$Api = Out-Value ApiUrl; $Bucket = Out-Value BucketName; $Table = Out-Value TableName
$Queue = Out-Value IntakeQueueUrl; $Dlq = Out-Value DeadLetterQueueUrl; $DlqArn = Out-Value DeadLetterQueueArn
$Key = aws secretsmanager get-secret-value --secret-id "$Stack/api-key" --region $Region --query SecretString --output text
```

## Health check (daily, 1 minute)

1. Open the `DashboardUrl` output. "Documents by outcome" should show traffic, the DLQ line should be 0, and Lambda errors 0.
2. `Invoke-RestMethod "$Api/health"` returns `status: ok`.
3. In the dashboard's "Latest rejections" table, a run of rejections from one uploader usually means a supplier changed format.

## Alarms — what to do

| Alarm | Meaning | Action |
|---|---|---|
| `docflow-dlq-not-empty` | A message failed 3 times | See **DLQ** below |
| `docflow-intake-backlog` | Oldest upload waiting > 5 min | Check `docflow-processor` errors and throttles on the dashboard. If throttled, raise `MaximumConcurrency` in the template. If erroring, read the logs (query below) |
| `docflow-processor-errors` | The Lambda crashed or timed out (a code or permissions problem, not a bad document) | Logs Insights on `/aws/lambda/docflow-processor`: `filter level = "ERROR" \| fields ts, msg, exc \| sort ts desc` |
| `docflow-api-5xx` | The API returned server errors | `filter msg = "unhandled error"` on `/aws/lambda/docflow-api`, then match `request_id` with the client's `x-request-id` header |
| `docflow-rejection-spike` | ≥ 5 rejections in 15 min | Not an outage. Contact the uploader or supplier shown in the "Latest rejections" table |

## DLQ: inspect and redrive

```powershell
# what is in it
aws sqs receive-message --queue-url $Dlq --region $Region --max-number-of-messages 10 --visibility-timeout 0 --query "Messages[].Body"
# after fixing the cause, move everything back to the intake queue (processing is idempotent, so this is safe)
aws sqs start-message-move-task --source-arn $DlqArn --region $Region
```

## Reprocess a quarantined document

A quarantined file is never reprocessed automatically. Its record is final. Ask the uploader to fix the file (the
`reason` says what is wrong) and upload it again, which creates a new `document_id`. To inspect it:
`aws s3 cp "s3://$Bucket/quarantine/<doc_id>/<name>" .` (never open it on a production machine).

## Rotate the API key

```powershell
$new = aws secretsmanager get-random-password --password-length 40 --exclude-punctuation --query RandomPassword --output text
aws secretsmanager put-secret-value --secret-id "$Stack/api-key" --secret-string $new --region $Region
```

Warm Lambdas keep the old key for up to 5 minutes, so give clients the new key and expect the switch within 5 minutes.

## Backup & recovery

| Data | Protection | RPO | Recovery |
|---|---|---|---|
| Document files (S3) | Versioning. Old versions kept 30 days | 0 (every write is a version) | Restore a deleted/overwritten object, see R1 |
| Records (DynamoDB) | Point-in-time recovery, 35 days | ~5 min (PITR granularity is seconds; 5 min is conservative) | Restore to a new table, see R2 |
| Whole stack | Template in Git, teardown backup | — | `deploy.ps1` (≈ 3 min) + R2 from the teardown backup |

**RTO target ≤ 30 min.** A DynamoDB restore to a new table takes most of that time.

**R1 — restore a deleted document file**

```powershell
$k = "processed/2026/11/<doc_id>/<name>"
aws s3api list-object-versions --bucket $Bucket --prefix $k --region $Region --query "DeleteMarkers[?IsLatest].VersionId" --output text
aws s3api delete-object --bucket $Bucket --key $k --version-id <delete-marker-version-id> --region $Region   # removing the marker brings the file back
```

**R2 — restore the records table** (e.g. after a bad bulk change)

```powershell
aws dynamodb restore-table-to-point-in-time --source-table-name $Table --target-table-name "$Table-restored" `
    --restore-date-time 2026-11-01T10:00:00Z --region $Region
aws dynamodb wait table-exists --table-name "$Table-restored" --region $Region
```

Then either point the functions at it (`DOCS_TABLE` env var on both functions) or copy the needed items back. Note:
PITR and TTL settings are **not** copied to a restored table, so turn them on again.

### Restore drill (record results in test-evidence C10)

1. Upload a sample and note its `doc_id` and `storage_key`. Start a timer.
2. `aws s3 rm "s3://$Bucket/<storage_key>"`, then show that the download link now gives 404.
3. R1, then show the download works again. Note the time.
4. R2 to `-restored`, and `aws dynamodb get-item` the `doc_id` from the restored table. Stop the timer. **Measured RTO = ___ min.**
5. Delete the `-restored` table.

## Deploy, update, teardown

| Task | Command |
|---|---|
| First deploy / any code or template change | `.\scripts\deploy.ps1 -AlertEmail you@example.com` |
| Smoke test | `.venv\Scripts\python scripts\smoke_test.py --base-url $Api --api-key $Key` |
| Upload one file | `.venv\Scripts\python scripts\upload.py --base-url $Api --api-key $Key samples\INV-2026-0042.pdf` |
| Tear down (backup → empty bucket → delete) | `.\scripts\teardown.ps1` |
