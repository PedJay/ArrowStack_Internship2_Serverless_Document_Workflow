# Packages the Lambda code, then deploys (or updates) the whole stack with CloudFormation.
# Re-run it after any code change: `package` uploads a new zip only when the code changed.
# Usage:
#   .\scripts\deploy.ps1 -AlertEmail you@example.com
# Optional:
#   -EmailNotifyStatuses "REJECTED,DUPLICATE"   email only the exceptions, not every processed document
#   -NoRollback                                 keep failed resources for debugging
param(
    [Parameter(Mandatory = $true)][string]$AlertEmail,
    [string]$StackName = "docflow",
    [string]$Region = "ap-south-1",
    [string]$EmailNotifyStatuses = "PROCESSED,REJECTED,DUPLICATE",
    [int]$MaxUploadBytes = 10485760,
    [int]$MonthlyBudgetUsd = 5,
    [switch]$NoRollback
)
$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$template = Join-Path $root "infra\template.yaml"
$packaged = Join-Path $root "infra\packaged.yaml"

$account = aws sts get-caller-identity --query Account --output text
if ($LASTEXITCODE -ne 0) { throw "AWS CLI is not configured. Run 'aws configure' first." }

# 1. Private bucket for the Lambda zip (created once; not part of the stack, so packaging works before the stack exists)
$artifacts = "$StackName-artifacts-$account-$Region"
$exists = aws s3api list-buckets --query "Buckets[?Name=='$artifacts'].Name" --output text
if (-not $exists) {
    Write-Host "Creating artifact bucket $artifacts ..."
    aws s3api create-bucket --bucket $artifacts --region $Region --create-bucket-configuration LocationConstraint=$Region | Out-Null
    aws s3api put-public-access-block --bucket $artifacts --public-access-block-configuration `
        "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"
    # JSON via a file: inline JSON quoting differs between Windows PowerShell 5.1 and PowerShell 7
    $lifecycle = Join-Path $env:TEMP "docflow-artifacts-lifecycle.json"
    '{"Rules":[{"ID":"old-builds","Status":"Enabled","Filter":{},"Expiration":{"Days":30}}]}' |
        Set-Content -Path $lifecycle -Encoding ascii
    aws s3api put-bucket-lifecycle-configuration --bucket $artifacts --lifecycle-configuration "file://$lifecycle"
}

# 2. Zip src/ (only the docflow package; no caches) and rewrite Code: ../src to the S3 location
Get-ChildItem (Join-Path $root "src") -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
aws cloudformation package `
    --template-file $template `
    --s3-bucket $artifacts `
    --s3-prefix lambda `
    --output-template-file $packaged `
    --region $Region | Out-Null
if ($LASTEXITCODE -ne 0) { throw "cloudformation package failed" }

# 3. Deploy
$extra = @()
if ($NoRollback) { $extra += "--disable-rollback" }
Write-Host "Deploying stack '$StackName' to $Region (first deploy takes ~3 minutes)..."
aws cloudformation deploy `
    --template-file $packaged `
    --stack-name $StackName `
    --region $Region `
    --capabilities CAPABILITY_IAM `
    --no-fail-on-empty-changeset `
    --tags Project=$StackName Owner=arrowstack-intern `
    @extra `
    --parameter-overrides `
        "ProjectName=$StackName" `
        "AlertEmail=$AlertEmail" `
        "EmailNotifyStatuses=$EmailNotifyStatuses" `
        "MaxUploadBytes=$MaxUploadBytes" `
        "MonthlyBudgetUsd=$MonthlyBudgetUsd"
if ($LASTEXITCODE -ne 0) { throw "CloudFormation deploy failed - check the Events tab in the console (RUN-GUIDE troubleshooting)." }

aws cloudformation describe-stacks --stack-name $StackName --region $Region `
    --query "Stacks[0].Outputs[].[OutputKey,OutputValue]" --output table

Write-Host ""
Write-Host "Next: confirm BOTH SNS subscription emails (document events + alarms), then run the smoke test:"
Write-Host "  `$api = aws cloudformation describe-stacks --stack-name $StackName --region $Region --query `"Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue`" --output text"
Write-Host "  `$key = aws secretsmanager get-secret-value --secret-id $StackName/api-key --region $Region --query SecretString --output text"
Write-Host "  .venv\Scripts\python scripts\smoke_test.py --base-url `$api --api-key `$key"
