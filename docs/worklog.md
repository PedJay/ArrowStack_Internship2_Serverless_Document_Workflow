# Work Log — Project 2

Keep this dated and honest. Mentors grade "problem-solving & independence" from entries like these.

| Date | Hours | Work done | Problems & how solved | Next |
|---|---|---|---|---|
| 2026-10-31 | | Read the handbook's Project 2 brief. Framed it as BrightMart's supplier-document intake for Accounts Payable (continuing Project 1's business). Wrote requirements, architecture, decision log. Built the api and processor Lambdas (stdlib-only inspection, duplicate claim, idempotent filing), the CloudFormation template (31 resources), deploy/teardown/smoke/demo scripts, sample files. 84 tests, 94% coverage. Local demo 14/14 | Bucket ↔ queue-policy circular dependency, solved with a known bucket name (D10). Realised a missing `s3:ListBucket` turns 404 into 403 and would send redeliveries to the DLQ (D17). Tests caught a metrics crash after filing (worklog defect #1). The demo showed an unhelpful reason for Latin-1 text (D18). Caught a CloudFormation "multiplication" by string concatenation (D16) | Deploy to AWS (on hold until tonight) |
| 2026-10-31 | | Deployed to AWS (ap-south-1) in ~3 min, in parallel with Project 1. Cloud smoke test 26/26 PASS | Measured end-to-end latency ~22 s although processing took < 1 s. Traced it with the EMF metrics to the SQS batching window, set it to 0, redeployed: p50 1.2 s (D21) | Screenshots, video, drills |
| | | Cloud smoke test, upload demo, dashboard + email screenshots (C1–C6) | | |
| | | Security evidence (C7–C8, C11). DLQ drill (C9). Restore drill (C10) | | |
| | | Presentation + demo rehearsal. Teardown | | |
