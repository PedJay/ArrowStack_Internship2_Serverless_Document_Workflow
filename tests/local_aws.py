"""Creates the same resources as infra/template.yaml inside moto (in-memory fake AWS).

Used by the tests and by scripts/local_demo.py. Keep the table keys/index and the S3 -> SQS
notification in sync with the CloudFormation template.
"""
from __future__ import annotations

import json
import os

import boto3

REGION = "ap-south-1"
BUCKET = "docflow-docs-test"
TABLE = "docflow-documents-test"
QUEUE = "docflow-intake-test"
TOPIC = "docflow-document-events-test"
API_KEY = "test-key"


def set_env() -> None:
    os.environ.update(
        {
            "AWS_ACCESS_KEY_ID": "testing",
            "AWS_SECRET_ACCESS_KEY": "testing",
            "AWS_SECURITY_TOKEN": "testing",
            "AWS_SESSION_TOKEN": "testing",
            "AWS_DEFAULT_REGION": REGION,
            "AWS_REGION": REGION,
            "APP_ENV": "test",
            "DOCS_BUCKET": BUCKET,
            "DOCS_TABLE": TABLE,
            "MAX_UPLOAD_BYTES": str(10 * 1024 * 1024),
            "LOG_LEVEL": "WARNING",
        }
    )


def create_resources() -> dict:
    """Call inside an active moto `mock_aws()` context. Returns ids and sets the env vars that point at them."""
    s3 = boto3.client("s3", region_name=REGION)
    sqs = boto3.client("sqs", region_name=REGION)
    ddb = boto3.client("dynamodb", region_name=REGION)
    sns = boto3.client("sns", region_name=REGION)
    sm = boto3.client("secretsmanager", region_name=REGION)

    dlq_url = sqs.create_queue(QueueName=f"{QUEUE}-dlq")["QueueUrl"]
    dlq_arn = sqs.get_queue_attributes(QueueUrl=dlq_url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    queue_url = sqs.create_queue(
        QueueName=QUEUE,
        Attributes={
            "VisibilityTimeout": "180",
            "RedrivePolicy": json.dumps({"deadLetterTargetArn": dlq_arn, "maxReceiveCount": "3"}),
        },
    )["QueueUrl"]
    queue_arn = sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]

    s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
    s3.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})
    s3.put_bucket_notification_configuration(
        Bucket=BUCKET,
        NotificationConfiguration={
            "QueueConfigurations": [
                {
                    "QueueArn": queue_arn,
                    "Events": ["s3:ObjectCreated:*"],
                    "Filter": {"Key": {"FilterRules": [{"Name": "prefix", "Value": "incoming/"}]}},
                }
            ]
        },
    )

    ddb.create_table(
        TableName=TABLE,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "doc_id", "AttributeType": "S"},
            {"AttributeName": "status", "AttributeType": "S"},
            {"AttributeName": "created_at", "AttributeType": "S"},
        ],
        KeySchema=[{"AttributeName": "doc_id", "KeyType": "HASH"}],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "by_status",
                "KeySchema": [
                    {"AttributeName": "status", "KeyType": "HASH"},
                    {"AttributeName": "created_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    )

    topic_arn = sns.create_topic(Name=TOPIC)["TopicArn"]
    # An SQS subscriber stands in for the email inbox so tests can read notifications
    inbox_url = sqs.create_queue(QueueName=f"{TOPIC}-inbox")["QueueUrl"]
    inbox_arn = sqs.get_queue_attributes(QueueUrl=inbox_url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    sns.subscribe(TopicArn=topic_arn, Protocol="sqs", Endpoint=inbox_arn, Attributes={"RawMessageDelivery": "false"})

    secret_arn = sm.create_secret(Name="docflow/api-key", SecretString=API_KEY)["ARN"]

    # S3 posts an "s3:TestEvent" to the queue when the notification is set up. Real S3 does this too.
    # The processor ignores it (see test_s3_test_event_is_ignored); drop it so each test starts empty
    drain(queue_url)

    os.environ["NOTIFY_TOPIC_ARN"] = topic_arn
    os.environ["API_KEY_SECRET_ARN"] = secret_arn
    os.environ.pop("API_KEY", None)
    return {"queue_url": queue_url, "dlq_url": dlq_url, "inbox_url": inbox_url, "topic_arn": topic_arn, "secret_arn": secret_arn}


def drain(queue_url: str, max_messages: int = 10) -> list[dict]:
    """Receive (and delete) everything currently in a queue, as SQS-event records for the processor."""
    sqs = boto3.client("sqs", region_name=REGION)
    records: list[dict] = []
    while True:
        msgs = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=max_messages).get("Messages", [])
        if not msgs:
            return records
        for m in msgs:
            records.append({"messageId": m["MessageId"], "receiptHandle": m["ReceiptHandle"], "body": m["Body"]})
            sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=m["ReceiptHandle"])
