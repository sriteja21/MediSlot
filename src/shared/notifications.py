"""Persist and deliver idempotent patient and doctor email notifications."""

import logging
import os
import time
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError


logger = logging.getLogger(__name__)
_dynamodb = None
_ses = None


def _notifications_table() -> Any:
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


def publish_email_notification(
    *,
    event_key: str,
    event_type: str,
    recipient_user_id: str,
    recipient_email: str,
    subject: str,
    message: str,
) -> None:
    """Write a deduplicated notification and send it through SES."""
    user_id = recipient_user_id.strip()
    email = recipient_email.strip()
    if not user_id or not email:
        logger.error(
            "Notification recipient is missing an account ID or email for event %s.",
            event_key,
        )
        return

    notification_id = f"{event_key}#{user_id}"
    now = int(time.time())
    item = {
        "recipient_user_id": user_id,
        "notification_id": notification_id,
        "event_type": event_type,
        "subject": subject,
        "message": message,
        "created_at": now,
        "expires_at": now + 60 * 60 * 24 * 90,
        "delivery_status": "PENDING",
    }
    try:
        _notifications_table().put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(recipient_user_id)",
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            logger.info("Skipping duplicate notification event %s.", event_key)
            return
        raise

    sender = os.environ.get("SES_FROM_EMAIL", "").strip()
    if not sender:
        _set_delivery_status(user_id, notification_id, "BLOCKED")
        logger.error("SES_FROM_EMAIL is not configured; notification %s was persisted but not emailed.", notification_id)
        return

    try:
        _ses_client().send_email(
            Source=sender,
            Destination={"ToAddresses": [email]},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {"Text": {"Data": message, "Charset": "UTF-8"}},
            },
        )
    except (BotoCoreError, ClientError):
        _set_delivery_status(user_id, notification_id, "FAILED")
        logger.exception("SES delivery failed for notification %s.", notification_id)
        return
    _set_delivery_status(user_id, notification_id, "SENT")


def publish_appointment_notification(
    *,
    appointment_id: str,
    event_type: str,
    patient_id: str,
    consultation_type_id: str,
    appointment_datetime: Any,
    message: str,
    patient_only: bool = False,
) -> None:
    """Notify the appointment's patient and doctor through the shared SES publisher."""
    from shared.database import execute_query

    rows = execute_query(
        """
        SELECT pu.user_id AS patient_user_id, pu.email AS patient_email,
               du.user_id AS doctor_user_id, du.email AS doctor_email,
               CONCAT_WS(' ', d.first_name, d.last_name) AS doctor_name
        FROM patients p
        JOIN users pu ON pu.user_id = p.user_id
        JOIN consultation_types ct ON ct.consultation_type_id = %s
        JOIN doctors d ON d.doctor_id = ct.doctor_id
        JOIN users du ON du.user_id = d.user_id
        WHERE p.patient_id = %s
        LIMIT 1
        """,
        (consultation_type_id, patient_id),
    )
    if not rows:
        logger.error("Could not resolve notification recipients for appointment %s.", appointment_id)
        return
    contacts = rows[0]
    timestamp = appointment_datetime.isoformat()
    recipients = [
        (contacts["patient_user_id"], contacts["patient_email"], "patient"),
    ]
    if not patient_only:
        recipients.append((contacts["doctor_user_id"], contacts["doctor_email"], "doctor"))
    for user_id, email, recipient_role in recipients:
        publish_email_notification(
            event_key=f"{appointment_id}#{event_type}",
            event_type=event_type,
            recipient_user_id=str(user_id),
            recipient_email=str(email),
            subject=f"MediSlot appointment {event_type.replace('_', ' ').lower()}",
            message=(
                f"{message}\nDoctor: {contacts['doctor_name']}\n"
                f"Appointment: {timestamp}\nRecipient: {recipient_role}"
            ),
        )


def _set_delivery_status(
    recipient_user_id: str,
    notification_id: str,
    status: str,
) -> None:
    _notifications_table().update_item(
        Key={
            "recipient_user_id": recipient_user_id,
            "notification_id": notification_id,
        },
        UpdateExpression="SET delivery_status = :status",
        ExpressionAttributeValues={":status": status},
    )
