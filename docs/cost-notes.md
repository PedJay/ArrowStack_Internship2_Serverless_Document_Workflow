# Cost Notes — DocFlow

Design rule: **pay per use, nothing always-on.** When nobody uploads, almost nothing is billed. Compare Project 1, whose
EC2 + RDS instances run 24/7.

Prices are indicative (AWS public pricing as of late 2026). Check the AWS Pricing Calculator for `ap-south-1` before quoting
them. The account is on the AWS free plan with credits, so any usage draws down credits first.

| Service | What we use | Free tier / allowance | Expected demo cost |
|---|---|---|---|
| Lambda (arm64) | 2 functions, ~0.1–0.5 s each | 1M requests + 400,000 GB-s per month (always free) | $0 |
| API Gateway HTTP API | a few hundred requests | 1M calls/month (first 12 months on legacy free tier) | $0 (≈ $1 per million after) |
| S3 Standard | a few MB, versioned | 5 GB (legacy free tier) | $0 |
| SQS | ~3 requests per document + Lambda polling | 1M requests/month (always free) | $0. Note: the trigger long-polls even when idle (well under 1M/month at max concurrency 2) |
| DynamoDB on-demand | ~4 writes + reads per document | 25 GB storage free. On-demand requests are **not** in the free tier | cents |
| DynamoDB PITR | tiny table | — | cents (billed per GB-month) |
| SNS | 1 publish + email per document | 1M publishes, 1,000 emails/month | $0 |
| Secrets Manager | 1 secret | 30-day trial for a new secret | **≈ $0.40/month** (the only fixed cost) |
| CloudWatch | 5 custom metrics, 5 alarms, 1 dashboard, logs (14-day retention) | 10 metrics, 10 alarms, 3 dashboards, 5 GB logs | $0 |
| AWS Budgets | 1 budget | first 2 free | $0 |

**Estimate:** < **$1/month** at demo volume, with Secrets Manager the main item. Budget alert at 80% of **$5**, plus a
forecast alert at 100%.

## Cost controls built in

* `MaximumConcurrency: 2` on the queue trigger and 10 rps API throttling cap the worst case if something loops or is abused.
* Lifecycle rules: abandoned `incoming/` objects deleted after 1 day, quarantine after 30 days, old versions after 30 days,
  `processed/` moves to Standard-IA after 90 days.
* Log retention 14 days on all three log groups (CloudWatch's default is "never expire").
* No NAT gateway, no VPC (Lambda doesn't need one here). That saves ~$32/month compared with a VPC-attached design.
* arm64 Lambdas are about 20% cheaper than x86.
* The deploy script's artifact bucket expires old zips after 30 days.

## Teardown leftovers (small, deliberate)

* The DynamoDB on-demand backup from `teardown.ps1` (cents per GB-month). Delete it when it's no longer needed (command printed by the script).
* Nothing else: the artifact bucket is deleted unless `-KeepArtifacts` is given, and the secret is force-deleted.

## At 10× / production

DynamoDB on-demand stays cheaper until traffic is steady. Then provisioned capacity with auto-scaling wins. Textract
(roadmap) would be the dominant cost per page, so it should run only on documents classified as invoices.
