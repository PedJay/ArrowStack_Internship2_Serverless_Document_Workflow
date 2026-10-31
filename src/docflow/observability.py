"""Structured JSON logs and CloudWatch metrics.

Logs are one JSON object per line so they can be queried in CloudWatch Logs Insights.
Metrics use the CloudWatch Embedded Metric Format (EMF): a specially shaped log line
that CloudWatch turns into a metric. There is no PutMetricData call and no agent.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime, timezone

from docflow.config import get_settings


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        payload.update(getattr(record, "extra_fields", {}))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str | None = None) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level or get_settings().log_level)
    # boto's own debug chatter is never useful in these logs
    logging.getLogger("botocore").setLevel(logging.WARNING)


def log(logger: logging.Logger, level: int, msg: str, **fields) -> None:
    logger.log(level, msg, extra={"extra_fields": fields})


def emit_metrics(service: str, metrics: dict[str, tuple[float, str]], **properties) -> None:
    """Print one EMF record. metrics = {name: (value, unit)}; properties are searchable log fields."""
    record = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": get_settings().metrics_namespace,
                    "Dimensions": [["Service"]],
                    "Metrics": [{"Name": name, "Unit": unit} for name, (_, unit) in metrics.items()],
                }
            ],
        },
        "Service": service,
        **{name: value for name, (value, _) in metrics.items()},
        **properties,
    }
    # print, not logging: EMF needs the raw JSON line with no formatter wrapping it
    print(json.dumps(record, default=str), flush=True)
