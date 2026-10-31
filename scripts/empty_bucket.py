"""Deletes every object version and delete marker in a bucket (CloudFormation can't delete a non-empty bucket).

Used by teardown.ps1. Prints what it would delete and asks first unless --yes is given.
"""
from __future__ import annotations

import argparse
import sys

import boto3


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--region", required=True)
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    s3 = boto3.client("s3", region_name=args.region)
    targets = []
    for page in s3.get_paginator("list_object_versions").paginate(Bucket=args.bucket):
        for v in page.get("Versions", []) + page.get("DeleteMarkers", []):
            targets.append({"Key": v["Key"], "VersionId": v["VersionId"]})
    if not targets:
        print(f"{args.bucket} is already empty")
        return 0
    if not args.yes and input(f"Permanently delete {len(targets)} object versions from {args.bucket}? [y/N] ").lower() != "y":
        print("Aborted.")
        return 1
    for i in range(0, len(targets), 1000):
        s3.delete_objects(Bucket=args.bucket, Delete={"Objects": targets[i : i + 1000], "Quiet": True})
    print(f"Deleted {len(targets)} object versions from {args.bucket}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
