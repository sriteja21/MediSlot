"""Queue notification events for the dedicated notification dispatcher."""

import hashlib
import json
import logging
import os
from typing import Any

import boto3


logger = logging.getLogger(__name__)
_sqs = None
_events = None

DETAIL_TYPES = {
    "APPOINTMENT_BOOKED": "AppointmentBooked",
    "APPOINTMENT_CANCELLED_BY_PATIENT": "AppointmentCancelledByPatient",
    "APPOINTMENT_CANCELLED_BY_DOCTOR": "AppointmentCancelledByDoctor",
    "APPOINTMENT_RESCHEDULED": "AppointmentRescheduled",
    "WAITLIST_OFFER_ISSUED": "WaitlistOfferIssued",
    "REMINDER_24H": "AppointmentReminder24h",
    "REMINDER_2H": "AppointmentReminder2h",
    "NO_SHOW": "AppointmentNoShow",
}


def _queue_client() -> Any:
    global _sqs
    if _sqs is None:
        _sqs = boto3.client(
            "sqs",
            region_name=os.environ.get("AWS_REGION", "ap-south-1"),
        )
    return _sqs


def publish_domain_event(detail_type: str, detail: dict[str, Any], source: str = "medislot.appointments") -> None:
    global _events
    if _events is None:
        _events = boto3.client("events", region_name=os.environ.get("AWS_REGION", "ap-south-1"))
    response = _events.put_events(Entries=[{
        "EventBusName": os.environ["EVENT_BUS_NAME"],
        "Source": source,
        "DetailType": detail_type,
        "Detail": json.dumps(detail, default=str),
    }])
    if response.get("FailedEntryCount"):
        raise RuntimeError(f"EventBridge rejected the event: {response.get('Entries', [])}")


def publish_email_notification(
    *,
    event_key: str,
    event_type: str,
    recipient_user_id: str,
    recipient_email: str,
    subject: str,
    message: str,
) -> None:
    """Enqueue an email event. This module never writes notification records or sends email."""
    event_key = event_key.strip()
    event_type = event_type.strip()
    user_id = recipient_user_id.strip()
    email = recipient_email.strip()
    queue_url = os.environ.get("NOTIFICATION_QUEUE_URL", "").strip()
    if (
        not event_key
        or not event_type
        or not user_id
        or not email
        or not subject.strip()
        or not message.strip()
        or not queue_url
    ):
        raise RuntimeError("Notification queue or recipient configuration is missing.")

    notification_id = f"{event_key}#{user_id}"
    payload = {
        "notification_id": notification_id,
        "event_type": event_type,
        "recipient_user_id": user_id,
        "recipient_email": email,
        "subject": subject,
        "message": message,
    }
    deduplication_id = hashlib.sha256(notification_id.encode("utf-8")).hexdigest()
    _queue_client().send_message(
        QueueUrl=queue_url,
        MessageBody=json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
        MessageGroupId=user_id,
        MessageDeduplicationId=deduplication_id,
    )


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
    """Resolve appointment recipients and enqueue an event for each recipient."""
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
        logger.warning("Notification recipient lookup returned no rows.")
        return

    contacts = rows[0]
    timestamp = appointment_datetime.isoformat()
    recipients = [
        (contacts["patient_user_id"], contacts["patient_email"], "patient"),
    ]
    if not patient_only:
        recipients.append((contacts["doctor_user_id"], contacts["doctor_email"], "doctor"))

    publish_domain_event(
        DETAIL_TYPES[event_type],
        {
            "event_key": f"{appointment_id}#{event_type}",
            "appointment_id": str(appointment_id),
            "consultation_type_id": str(consultation_type_id),
            "appointment_datetime": timestamp,
            "message": message,
            "doctor_name": contacts["doctor_name"],
            "recipients": [
                {"user_id": str(user_id), "email": str(email), "role": role}
                for user_id, email, role in recipients
            ],
        },
    )
