"""boto3 clients, created once per Lambda container (warm invocations reuse them)."""
from __future__ import annotations

from functools import lru_cache

import boto3
from botocore.config import Config

from docflow.config import get_settings

_RETRIES = Config(retries={"max_attempts": 5, "mode": "standard"})


@lru_cache
def s3():
    # SigV4 + virtual-hosted URLs: presigned URLs must point at the regional endpoint,
    # otherwise S3 answers a presigned POST with a 307 redirect for new buckets.
    cfg = _RETRIES.merge(Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))
    return boto3.client("s3", region_name=get_settings().region, config=cfg)


@lru_cache
def dynamodb_table():
    return boto3.resource("dynamodb", region_name=get_settings().region, config=_RETRIES).Table(get_settings().table)


@lru_cache
def sns():
    return boto3.client("sns", region_name=get_settings().region, config=_RETRIES)


@lru_cache
def secretsmanager():
    return boto3.client("secretsmanager", region_name=get_settings().region, config=_RETRIES)


def reset_clients() -> None:
    for fn in (s3, dynamodb_table, sns, secretsmanager):
        fn.cache_clear()
