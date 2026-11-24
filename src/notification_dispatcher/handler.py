"""SQS consumer that persists and delivers MediSlot email notifications."""

import hashlib
import json
import logging
import os
import time
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError


logger = logging.getLogger(__name__)
_dynamodb = None
_ses = None


def _table() -> Any:
    global _dynamodb
    table_name = os.environ.get("NOTIFICATIONS_TABLE", "").strip()
    if not table_name:
        raise RuntimeError("NOTIFICATIONS_TABLE is not configured.")
    if _dynamodb is None:
        _dynamodb = boto3.resource(
            "dynamodb",
            region_name=os.environ.get("AWS_REGION", "ap-south-1"),
        )
    return _dynamodb.Table(table_name)


def _ses_client() -> Any:
    global _ses
    if _ses is None:
        _ses = boto3.client(
            "ses",
            region_name=os.environ.get("AWS_REGION", "ap-south-1"),
        )
    return _ses


def _notification_from_record(record: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = json.loads(record.get("body") or "")
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Notification queue message is not valid JSON.") from exc

    required = (
        "notification_id",
        "event_type",
        "recipient_user_id",
        "recipient_email",
        "subject",
        "message",
    )
    if any(not isinstance(payload.get(key), str) or not payload[key].strip() for key in required):
        raise ValueError("Notification queue message is missing required fields.")
    return payload


def _persist_pending(notification: dict[str, Any], now: int) -> dict[str, Any]:
    table = _table()
    recipient_id = notification["recipient_user_id"]
    notification_id = notification["notification_id"]
    item = {
        "recipient_user_id": recipient_id,
        "notification_id": notification_id,
        "event_type": notification["event_type"],
        "message": notification["message"],
        "recipient_email": notification["recipient_email"],
        "subject": notification["subject"],
        "status": "PENDING",
        "retry_count": 0,
        "retry_shard": f"R#{int(hashlib.sha256(notification_id.encode()).hexdigest()[:8], 16) % 10}",
        "next_attempt_at": now,
        "created_at": now,
        "expires_at": now + 60 * 60 * 24 * 90,
    }
    try:
        table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(recipient_user_id)",
        )
        return item
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise

    existing = table.get_item(
        Key={
            "recipient_user_id": recipient_id,
            "notification_id": notification_id,
        },
        ConsistentRead=True,
    ).get("Item")
    if not existing:
        raise RuntimeError("Notification record disappeared after duplicate detection.")
    return existing


def _mark_sent(notification: dict[str, Any], sent_at: int) -> None:
    _table().update_item(
        Key={
            "recipient_user_id": notification["recipient_user_id"],
            "notification_id": notification["notification_id"],
        },
        UpdateExpression="SET #status = :sent, sent_at = :sent_at REMOVE retry_shard, next_attempt_at",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":sent": "SENT", ":sent_at": sent_at},
    )


def _mark_failed(notification: dict[str, Any], now: int) -> None:
    notification_id = notification["notification_id"]
    shard = int(hashlib.sha256(notification_id.encode()).hexdigest()[:8], 16) % 10
    _table().update_item(
        Key={
            "recipient_user_id": notification["recipient_user_id"],
            "notification_id": notification_id,
        },
        UpdateExpression=(
            "SET #status = :failed, "
            "retry_count = if_not_exists(retry_count, :zero) + :one, "
            "retry_shard = :shard, next_attempt_at = :next_attempt"
        ),
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":failed": "FAILED",
            ":zero": 0,
            ":one": 1,
            ":shard": f"R#{shard}",
            ":next_attempt": now + 60,
        },
    )


def _deliver(notification: dict[str, Any]) -> None:
    if notification.get("status") == "SENT":
        return
    sender = os.environ.get("SES_FROM_EMAIL", "").strip()
    if not sender:
        raise RuntimeError("SES_FROM_EMAIL is not configured.")
    _ses_client().send_email(
        Source=sender,
        Destination={"ToAddresses": [notification["recipient_email"]]},
        Message={
            "Subject": {"Data": notification["subject"], "Charset": "UTF-8"},
            "Body": {"Text": {"Data": notification["message"], "Charset": "UTF-8"}},
        },
    )
    _mark_sent(notification, int(time.time()))


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, list[dict[str, str]]]:
    failures: list[dict[str, str]] = []
    records = event.get("Records", [])
    logger.info("Notification dispatcher received %d queue records.", len(records))
    for record in records:
        message_id = str(record.get("messageId") or "")
        try:
            notification = _notification_from_record(record)
            now = int(time.time())
            persisted = _persist_pending(notification, now)
            if persisted.get("status") == "SENT":
                continue
            try:
                _deliver({**notification, **persisted})
            except (BotoCoreError, ClientError, RuntimeError) as exc:
                _mark_failed(notification, int(time.time()))
                logger.error(
                    "Notification delivery failed; queue retry will be used (%s): %s",
                    type(exc).__name__,
                    exc,
                )
                if message_id:
                    failures.append({"itemIdentifier": message_id})
        except Exception as exc:
            logger.error(
                "Notification dispatcher could not process a queue record (%s).",
                type(exc).__name__,
            )
            if message_id:
                failures.append({"itemIdentifier": message_id})
    return {"batchItemFailures": failures}
