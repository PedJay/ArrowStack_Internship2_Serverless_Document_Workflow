"""Settings for both Lambda functions.

Everything comes from environment variables that CloudFormation sets. The API key
is NOT one of them: the function receives only the ARN of a Secrets Manager
secret and reads the value with its execution role. The value is cached for a few
minutes, so a rotated key takes effect without a redeploy.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from functools import lru_cache

LOCAL_DEV_API_KEY = "local-dev-key"
API_KEY_TTL_SECONDS = 300

# Declared content type -> allowed file extensions. This is the whole allow-list.
ALLOWED_TYPES: dict[str, tuple[str, ...]] = {
    "application/pdf": (".pdf",),
    "image/png": (".png",),
    "image/jpeg": (".jpg", ".jpeg"),
    "text/plain": (".txt",),
    "text/csv": (".csv",),
}


@dataclass(frozen=True)
class Settings:
    environment: str
    region: str
    bucket: str
    table: str
    topic_arn: str | None
    max_upload_bytes: int
    upload_url_ttl_seconds: int
    download_url_ttl_seconds: int
    metrics_namespace: str
    log_level: str


@lru_cache
def get_settings() -> Settings:
    return Settings(
        environment=os.getenv("APP_ENV", "local"),
        region=os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "ap-south-1",
        bucket=os.environ["DOCS_BUCKET"],
        table=os.environ["DOCS_TABLE"],
        topic_arn=os.getenv("NOTIFY_TOPIC_ARN") or None,
        max_upload_bytes=int(os.getenv("MAX_UPLOAD_BYTES", str(10 * 1024 * 1024))),
        upload_url_ttl_seconds=int(os.getenv("UPLOAD_URL_TTL_SECONDS", "300")),
        download_url_ttl_seconds=int(os.getenv("DOWNLOAD_URL_TTL_SECONDS", "300")),
        metrics_namespace=os.getenv("METRICS_NAMESPACE", "DocFlow"),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
    )


_key_lock = threading.Lock()
_key_cache: tuple[float, str] | None = None


def get_api_key() -> str:
    """The shared API key, from Secrets Manager in AWS or an env var locally."""
    global _key_cache
    with _key_lock:
        if _key_cache and time.monotonic() - _key_cache[0] < API_KEY_TTL_SECONDS:
            return _key_cache[1]
        secret_arn = os.getenv("API_KEY_SECRET_ARN")
        if secret_arn:
            from docflow import aws

            key = aws.secretsmanager().get_secret_value(SecretId=secret_arn)["SecretString"].strip()
        elif os.getenv("API_KEY"):
            key = os.environ["API_KEY"]
        elif get_settings().environment == "local":
            key = LOCAL_DEV_API_KEY
        else:
            raise RuntimeError("API key is not configured (set API_KEY_SECRET_ARN or API_KEY)")
        _key_cache = (time.monotonic(), key)
        return key


def reset_caches() -> None:
    """Used by tests and the local demo to start from a clean slate."""
    global _key_cache
    get_settings.cache_clear()
    with _key_lock:
        _key_cache = None
