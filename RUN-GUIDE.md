# RUN-GUIDE — Project 2 cloud deployment (the on-hold part)

Everything except the AWS steps is done and verified locally. This is the checklist for the deployment session.
It takes about **60–75 min** including screenshots. The DLQ drill alone needs ~10 min of waiting.

Run everything in **PowerShell** from `E:\ArrowStack Internship\project2-serverless-document-workflow`.

## 0. Before you start (2 min)

```powershell
aws sts get-caller-identity                       # right account?
aws configure get region                          # the scripts use ap-south-1 regardless
.venv\Scripts\python -m pytest -q                 # 84 passed (sanity check that nothing changed)
```

> The account is on the AWS **free plan** (credits). If any step fails with `SubscriptionRequiredException` or
> "not available on your plan", **stop**. Note which service it was and decide before upgrading (upgrading can't be undone).
> All services used here (Lambda, HTTP API, S3, SQS, DynamoDB, SNS, Secrets Manager, CloudWatch, Budgets) are expected to work.

## 1. Deploy (≈ 5 min) → evidence C1

```powershell
.\scripts\deploy.ps1 -AlertEmail <your email>
```

* Screenshot the outputs table (**C1**) and the stack's Resources tab (31 resources, all `CREATE_COMPLETE`).
* Open your inbox and **confirm both** "AWS Notification - Subscription Confirmation" emails (DocFlow and DocFlow alarms).

## 2. Variables for the rest of the session

```powershell
$Stack = "docflow"; $Region = "ap-south-1"
function Out-Value($k) { aws cloudformation describe-stacks --stack-name $Stack --region $Region --query "Stacks[0].Outputs[?OutputKey=='$k'].OutputValue" --output text }
$Api = Out-Value ApiUrl; $Bucket = Out-Value BucketName; $Table = Out-Value TableName
$Queue = Out-Value IntakeQueueUrl; $Dlq = Out-Value DeadLetterQueueUrl; $DlqArn = Out-Value DeadLetterQueueArn
$Key = aws secretsmanager get-secret-value --secret-id "$Stack/api-key" --region $Region --query SecretString --output text
```

## 3. Smoke test (≈ 3 min) → C2, C13

```powershell
.venv\Scripts\python scripts\smoke_test.py --base-url $Api --api-key $Key
```

Expect every line to be `PASS`. Screenshot the terminal (**C2**). The JSON report in `evidence/` includes
`observed_latency_ms` (**C13**: compare with the < 30 s target).

If a document stays `PENDING_UPLOAD`: check that the SNS/SQS setup finished (stack complete), then read the processor
logs: `aws logs tail /aws/lambda/docflow-processor --region $Region --since 10m`.

## 4. Demo uploads (≈ 3 min) → C3, C4

```powershell
.venv\Scripts\python scripts\upload.py --base-url $Api --api-key $Key samples\INV-2026-0042.pdf
.venv\Scripts\python scripts\upload.py --base-url $Api --api-key $Key "samples\INV-2026-0042 (resent).pdf"
.venv\Scripts\python scripts\upload.py --base-url $Api --api-key $Key samples\edge-cases\invoice_urgent.pdf
.venv\Scripts\python scripts\upload.py --base-url $Api --api-key $Key samples\stock_count_oct.csv
```

Note: if the smoke test already ran, the first upload is still **PROCESSED**. The smoke test embeds a run id, so its
invoice bytes differ from `samples\`. The resent copy becomes **DUPLICATE**, and the urgent "PDF" is **REJECTED** (Windows executable).

* Screenshot the first result showing `invoice_number` and `total_amount_minor: 1245050` (**C3**).
* Screenshot the PROCESSED, DUPLICATE and REJECTED emails (**C4**).

## 5. Console screenshots (≈ 15 min) → C5–C8, C11, C12

| # | Where | What to show |
|---|---|---|
| C5 | CloudWatch → Dashboards → `docflow-overview` | outcomes, latency p50/p95, queue, Lambda, API widgets (wait ~5 min after step 3 for metrics) |
| C6 | CloudWatch → Logs Insights, both `/aws/lambda/docflow-*` groups | `fields ts, msg, doc_id, status, request_id \| filter doc_id = "<id from step 4>" or msg = "request" \| sort ts asc \| limit 50` |
| C7 | IAM → Roles → `docflow-ApiFunctionRole-…` and `…ProcessorFunctionRole-…` | the inline `least-privilege` policies |
| C8 | S3 → `docflow-docs-…` → Permissions / Properties / Management | Block Public Access on, versioning on, SSE-S3, 4 lifecycle rules, event notification → SQS (`incoming/`) |
| C11 | DynamoDB → `docflow-documents` → Explore items | one PROCESSED, one REJECTED (with reason), one DUPLICATE (with `duplicate_of`), one `SHA256#…` claim item |
| C12 | Billing → Budgets | `docflow-monthly` $5 |

## 6. Alarm test (2 min) → C4

```powershell
aws cloudwatch set-alarm-state --alarm-name docflow-api-5xx --state-value ALARM --state-reason "Alarm notification test" --region $Region
```

Screenshot the email. The alarm returns to OK at the next evaluation.

## 7. DLQ drill (≈ 12 min, mostly waiting) → C9

```powershell
aws sqs send-message --queue-url $Queue --region $Region --message-body "this is not an S3 event"
```

The processor fails on it and the message becomes visible again after the 180 s visibility timeout. After the 3rd
receive (~9–10 min) it moves to the DLQ, and within 5 min the **`docflow-dlq-not-empty`** alarm email arrives.

```powershell
aws sqs get-queue-attributes --queue-url $Dlq --region $Region --attribute-names ApproximateNumberOfMessages
aws sqs receive-message --queue-url $Dlq --region $Region --visibility-timeout 0 --query "Messages[].Body"
# a real fix would come here; this message can never succeed, so purge it instead of redriving
aws sqs purge-queue --queue-url $Dlq --region $Region
```

Screenshots: processor ERROR log line, DLQ count = 1, alarm email, alarm back to OK (**C9**). (For a *real* failure the
fix is `aws sqs start-message-move-task --source-arn $DlqArn --region $Region`.)

## 8. Restore drill (≈ 15 min) → C10

Follow [docs/operations-runbook.md § Restore drill](docs/operations-runbook.md#restore-drill-record-results-in-test-evidence-c10).
Write the measured RTO into `docs/test-evidence.md` C10 and the runbook. **Delete the `-restored` table afterwards.**

## 9. Record results

* `docs/test-evidence.md` §4: fill in the result/file column.
* `docs/worklog.md`: add the deployment row (date, hours, problems, how solved).
* Save the screenshots in `evidence/` as `c1-stack-outputs.png`, `c2-smoke.png`, and so on.

## 10. Teardown (≈ 5 min) — when the demo/presentation is done

```powershell
.\scripts\teardown.ps1
```

This takes a DynamoDB backup, empties the bucket (all versions), deletes the stack, force-deletes the API-key secret
and deletes the artifact bucket. Afterwards, check that nothing is left:

```powershell
aws cloudformation describe-stacks --stack-name docflow --region ap-south-1     # "does not exist"
aws s3 ls | Select-String docflow                                               # nothing
aws dynamodb list-backups --region ap-south-1                                   # only the teardown backup (delete when no longer needed)
```

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `deploy` fails at `DocumentsBucket` with "Unable to validate the following destination configurations" | The queue policy wasn't ready yet. Re-run `deploy.ps1`. (The template already has `DependsOn: IntakeQueuePolicy`) |
| `deploy` fails: secret `docflow/api-key` "already scheduled for deletion" | A previous manual stack delete. Run `aws secretsmanager delete-secret --secret-id docflow/api-key --force-delete-without-recovery --region ap-south-1` and redeploy |
| Smoke test: S3 upload returns **307 / PermanentRedirect** | The presigned URL wasn't regional. The code already forces regional SigV4 URLs (`aws.py`). Check the `AWS_REGION` env var on the function |
| Every upload REJECTED "uploaded object no longer exists" | The processor role is missing `s3:ListBucket` (S3 returns 403). Check the role policy was deployed |
| No emails | The SNS subscriptions are still "PendingConfirmation". Confirm them. Check the spam folder |
| `ROLLBACK_COMPLETE` | Read the first `CREATE_FAILED` event, delete the stack (`aws cloudformation delete-stack ...`), fix, redeploy. Use `-NoRollback` to keep resources for debugging |
