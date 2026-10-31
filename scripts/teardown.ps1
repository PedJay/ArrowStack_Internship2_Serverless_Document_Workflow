# Deletes the stack. First takes an on-demand DynamoDB backup (kept after deletion, like a final
# RDS snapshot) and empties the versioned documents bucket, which CloudFormation cannot delete while non-empty.
param(
    [string]$StackName = "docflow",
    [string]$Region = "ap-south-1",
    [switch]$KeepArtifacts,   # keep the Lambda-zip bucket (faster next deploy)
    [switch]$SkipBackup
)
$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }

$answer = Read-Host "Delete stack '$StackName' in $Region and ALL uploaded documents? Type the stack name to confirm"
if ($answer -ne $StackName) { Write-Host "Aborted."; exit 1 }

function Get-Output($key) {
    aws cloudformation describe-stacks --stack-name $StackName --region $Region `
        --query "Stacks[0].Outputs[?OutputKey=='$key'].OutputValue" --output text
}
$bucket = Get-Output "BucketName"
$table = Get-Output "TableName"

if (-not $SkipBackup) {
    $backupName = "$StackName-final-" + (Get-Date -Format "yyyyMMdd-HHmm")
    Write-Host "Backing up table $table as $backupName ..."
    aws dynamodb create-backup --table-name $table --backup-name $backupName --region $Region `
        --query "BackupDetails.[BackupName,BackupStatus]" --output text
}

Write-Host "Emptying $bucket (all versions) ..."
& $python (Join-Path $root "scripts\empty_bucket.py") --bucket $bucket --region $Region --yes
if ($LASTEXITCODE -ne 0) { throw "Could not empty $bucket" }

aws cloudformation delete-stack --stack-name $StackName --region $Region
Write-Host "Waiting for deletion..."
aws cloudformation wait stack-delete-complete --stack-name $StackName --region $Region
if ($LASTEXITCODE -ne 0) { throw "Stack deletion did not complete - check the Events tab" }

# CloudFormation schedules the API-key secret for deletion in 30 days, and the name stays reserved
# until then. Remove it now so the stack can be redeployed straight away (the key is random; nothing is lost)
$ErrorActionPreference = "Continue"   # in Windows PowerShell, stderr from a native command would abort under "Stop"
aws secretsmanager delete-secret --secret-id "$StackName/api-key" --force-delete-without-recovery --region $Region 2>$null | Out-Null
$ErrorActionPreference = "Stop"

if (-not $KeepArtifacts) {
    $account = aws sts get-caller-identity --query Account --output text
    $artifacts = "$StackName-artifacts-$account-$Region"
    & $python (Join-Path $root "scripts\empty_bucket.py") --bucket $artifacts --region $Region --yes
    aws s3api delete-bucket --bucket $artifacts --region $Region
}

Write-Host "Stack deleted. DynamoDB backups kept:"
aws dynamodb list-backups --region $Region --table-name $table `
    --query "BackupSummaries[].[BackupName,BackupCreationDateTime,BackupSizeBytes]" --output table
Write-Host "Delete one with: aws dynamodb delete-backup --backup-arn <arn> --region $Region"
