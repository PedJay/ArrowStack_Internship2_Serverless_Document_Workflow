"""DynamoDB access for document records.

One table, keyed by `doc_id`:
  * document items:    doc_id = <uuid>,           status, filename, metadata ...
  * hash-claim items:  doc_id = "SHA256#<hex>",   owner_doc_id
The claim items have no `status`, so they never appear in the `by_status` index. A
conditional put on a claim item is how duplicates are detected without a race.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from decimal import Decimal

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from docflow import aws

PENDING = "PENDING_UPLOAD"
PROCESSED = "PROCESSED"
REJECTED = "REJECTED"
DUPLICATE = "DUPLICATE"
FINAL_STATUSES = (PROCESSED, REJECTED, DUPLICATE)
STATUS_INDEX = "by_status"

# A registered upload that never arrives is cleaned up by DynamoDB TTL after a day
PENDING_TTL_SECONDS = 24 * 3600


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _is_conditional_failure(exc: ClientError) -> bool:
    return exc.response["Error"]["Code"] == "ConditionalCheckFailedException"


def create_pending(item: dict) -> None:
    item = {**item, "status": PENDING, "created_at": now_iso(), "expires_at": int(time.time()) + PENDING_TTL_SECONDS}
    aws.dynamodb_table().put_item(Item=item, ConditionExpression="attribute_not_exists(doc_id)")


def get(doc_id: str) -> dict | None:
    return aws.dynamodb_table().get_item(Key={"doc_id": doc_id}, ConsistentRead=True).get("Item")


def list_by_status(status: str, limit: int, start_key: dict | None = None) -> tuple[list[dict], dict | None]:
    kwargs = {
        "IndexName": STATUS_INDEX,
        "KeyConditionExpression": Key("status").eq(status),
        "ScanIndexForward": False,  # newest first
        "Limit": limit,
    }
    if start_key:
        kwargs["ExclusiveStartKey"] = start_key
    resp = aws.dynamodb_table().query(**kwargs)
    return resp.get("Items", []), resp.get("LastEvaluatedKey")


def claim_hash(sha256: str, doc_id: str) -> str:
    """Record doc_id as the first holder of this content hash. Returns the doc_id that owns it.

    If it returns a different doc_id, this document is a duplicate. A retry of the same
    document gets its own doc_id back, so reprocessing is safe.
    """
    table = aws.dynamodb_table()
    try:
        table.put_item(
            Item={"doc_id": f"SHA256#{sha256}", "owner_doc_id": doc_id, "created_at": now_iso()},
            ConditionExpression="attribute_not_exists(doc_id)",
        )
        return doc_id
    except ClientError as exc:
        if not _is_conditional_failure(exc):
            raise
    return table.get_item(Key={"doc_id": f"SHA256#{sha256}"}, ConsistentRead=True)["Item"]["owner_doc_id"]


def complete(doc_id: str, status: str, fields: dict) -> bool:
    """Move a PENDING document to a final status. Returns False if another invocation already did.

    The condition makes a redelivered SQS message harmless: only the first one wins.
    """
    fields = {k: v for k, v in fields.items() if v is not None}
    fields.update({"status": status, "updated_at": now_iso()})
    names = {f"#{k}": k for k in fields}
    values = {f":{k}": _to_dynamo(v) for k, v in fields.items()}
    values[":pending"] = PENDING
    try:
        aws.dynamodb_table().update_item(
            Key={"doc_id": doc_id},
            UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in fields) + " REMOVE expires_at",
            ConditionExpression="attribute_exists(doc_id) AND #status = :pending",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
        return True
    except ClientError as exc:
        if _is_conditional_failure(exc):
            return False
        raise


def _to_dynamo(value):
    # DynamoDB rejects Python floats; everything we store is int/str/bool/list/dict
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_dynamo(v) for v in value]
    return value


def to_json(value):
    """Convert DynamoDB Decimals back to int/float for JSON responses."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: to_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_json(v) for v in value]
    return value
