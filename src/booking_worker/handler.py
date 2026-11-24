"""FIFO SQS consumer that books against the existing DynamoDB slot ledger."""

import json
import logging
import os
import random
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import boto3
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

from shared.database import database_transaction
from shared.notifications import publish_appointment_notification


logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())

AWS_REGION = os.environ.get("AWS_REGION", "ap-south-1")
SLOTS_TABLE_NAME = os.environ.get("SLOTS_TABLE", "").strip()
IDEMPOTENCY_TABLE_NAME = os.environ.get("IDEMPOTENCY_TABLE", "").strip()
_dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
_slots_table = _dynamodb.Table(SLOTS_TABLE_NAME) if SLOTS_TABLE_NAME else None
_idempotency_table = (
    _dynamodb.Table(IDEMPOTENCY_TABLE_NAME) if IDEMPOTENCY_TABLE_NAME else None
)
_serializer = TypeSerializer()
_dynamodb_client = _dynamodb.meta.client
TERMINAL_STATUSES = {"CONFIRMED", "DECLINED"}
MAX_RESERVATION_RETRIES = 8


class ExistingAppointmentError(Exception):
    """Raised when a patient already has an active appointment for the slot."""


class BookingAlreadyHandled(Exception):
    """Raised when a concurrent worker has already completed this request."""


def _serialize_values(values: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {key: _serializer.serialize(value) for key, value in values.items()}


def _request_from_message(record: dict[str, Any]) -> tuple[str, str]:
    body = json.loads(record.get("body") or "{}")
    user_id = str(body.get("user_id") or "")
    scope_key = str(body.get("scope_key") or "")
    if not user_id or not scope_key:
        raise ValueError("Booking message is missing its idempotency key.")
    return user_id, scope_key


def _get_request(user_id: str, scope_key: str) -> dict[str, Any] | None:
    if not _idempotency_table:
        raise RuntimeError("Idempotency table is not configured.")
    response = _idempotency_table.get_item(
        Key={"user_id": user_id, "scope_key": scope_key},
        ConsistentRead=True,
    )
    return response.get("Item")


def _get_slot(doctor_id: str, slot_sk: str) -> dict[str, Any] | None:
    if not _slots_table:
        raise RuntimeError("Slots table is not configured.")
    response = _slots_table.get_item(
        Key={"doctor_id": doctor_id, "slot_sk": slot_sk},
        ConsistentRead=True,
    )
    return response.get("Item")


def _slot_reservation_condition(
    slot: dict[str, Any],
) -> tuple[str, dict[str, Any], str]:
    capacity = int(slot["capacity"])
    booked = int(slot.get("booked_count", 0) or 0)
    held = int(slot.get("held_count", 0) or 0)
    if not slot.get("is_active") or slot.get("is_blocked") or booked + held >= capacity:
        raise ExistingAppointmentError("The selected slot is no longer available.")

    values: dict[str, Any] = {
        ":active": True,
        ":capacity": capacity,
        ":booked": booked,
        ":held": held,
        ":next_booked": booked + 1,
    }
    booked_condition = (
        "(attribute_not_exists(booked_count) OR booked_count = :booked)"
    )
    held_condition = (
        "(attribute_not_exists(held_count) OR held_count = :held)"
    )
    condition = (
        "is_active = :active AND #capacity = :capacity AND "
        f"{booked_condition} AND {held_condition}"
    )
    update = "SET booked_count = :next_booked"
    return condition, values, update


def _reserve_slot_once(request: dict[str, Any], slot: dict[str, Any]) -> None:
    if not _slots_table or not _idempotency_table:
        raise RuntimeError("Booking tables are not configured.")

    condition, values, update = _slot_reservation_condition(slot)
    table_names = {
        "slots": SLOTS_TABLE_NAME,
        "idempotency": IDEMPOTENCY_TABLE_NAME,
    }
    transaction = [
        {
            "Update": {
                "TableName": table_names["slots"],
                "Key": _serialize_values(
                    {
                        "doctor_id": str(request["doctor_id"]),
                        "slot_sk": str(request["slot_sk"]),
                    }
                ),
                "UpdateExpression": update,
                "ConditionExpression": condition,
                "ExpressionAttributeNames": {"#capacity": "capacity"},
                "ExpressionAttributeValues": _serialize_values(values),
            }
        },
        {
            "Update": {
                "TableName": table_names["idempotency"],
                "Key": _serialize_values(
                    {
                        "user_id": str(request["user_id"]),
                        "scope_key": str(request["scope_key"]),
                    }
                ),
                "UpdateExpression": (
                    "SET #status = :processing, slot_reserved = :true, "
                    "updated_at = :now"
                ),
                "ConditionExpression": (
                    "(#status = :pending OR #status = :processing) AND "
                    "(attribute_not_exists(slot_reserved) OR slot_reserved = :false)"
                ),
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": _serialize_values(
                    {
                        ":processing": "PROCESSING",
                        ":pending": "PENDING",
                        ":true": True,
                        ":false": False,
                        ":now": int(time.time()),
                    }
                ),
            }
        },
    ]
    _dynamodb_client.transact_write_items(TransactItems=transaction)


def _reserve_slot(request: dict[str, Any]) -> dict[str, Any]:
    if request.get("slot_reserved"):
        slot = _get_slot(str(request["doctor_id"]), str(request["slot_sk"]))
        if not slot:
            raise RuntimeError("Reserved slot record is missing.")
        return slot

    last_cancellation_reasons: list[dict[str, str]] = []
    for attempt in range(MAX_RESERVATION_RETRIES):
        slot = _get_slot(str(request["doctor_id"]), str(request["slot_sk"]))
        if not slot:
            raise ExistingAppointmentError("The selected slot no longer exists.")
        try:
            _reserve_slot_once(request, slot)
            request["slot_reserved"] = True
            request["status"] = "PROCESSING"
            return slot
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code")
            if error_code != "TransactionCanceledException":
                raise
            cancellation_reasons = exc.response.get("CancellationReasons") or []
            last_cancellation_reasons = [
                {
                    "code": str(reason.get("Code", "Unknown")),
                    "message": str(reason.get("Message", ""))[:300],
                }
                for reason in cancellation_reasons
            ]
            if any(reason["code"] == "ValidationError" for reason in last_cancellation_reasons):
                logger.error(
                    "Slot reservation transaction has a permanent validation error. "
                    "cancellation_reasons=%s",
                    last_cancellation_reasons,
                )
                raise RuntimeError(
                    "Slot reservation transaction failed validation: "
                    + "; ".join(
                        reason["message"] or reason["code"]
                        for reason in last_cancellation_reasons
                        if reason["code"] == "ValidationError"
                    )
                ) from exc
            latest = _get_request(str(request["user_id"]), str(request["scope_key"]))
            if latest and latest.get("slot_reserved"):
                request["slot_reserved"] = True
                return _get_slot(str(request["doctor_id"]), str(request["slot_sk"])) or slot
            if latest and str(latest.get("status", "")) in TERMINAL_STATUSES:
                request.update(latest)
                raise BookingAlreadyHandled(str(latest["status"])) from exc
            latest_slot = _get_slot(str(request["doctor_id"]), str(request["slot_sk"]))
            if latest_slot and int(latest_slot.get("booked_count", 0) or 0) + int(
                latest_slot.get("held_count", 0) or 0
            ) >= int(latest_slot.get("capacity", 0) or 0):
                raise ExistingAppointmentError(
                    "The selected slot is no longer available."
                ) from exc
            if attempt < MAX_RESERVATION_RETRIES - 1:
                time.sleep(min(0.05 * (2**attempt), 0.8) + random.uniform(0, 0.05))
    logger.error(
        "Slot reservation transaction kept cancelling. cancellation_reasons=%s retries=%d",
        last_cancellation_reasons,
        MAX_RESERVATION_RETRIES,
    )
    raise RuntimeError(
        "Could not safely reserve the selected slot after "
        f"{MAX_RESERVATION_RETRIES} attempts "
        f"(transaction cancellation reasons: {last_cancellation_reasons or 'unavailable'})."
    )


def _mark_declined_without_reservation(
    request: dict[str, Any],
    message: str,
) -> None:
    if not _idempotency_table:
        raise RuntimeError("Idempotency table is not configured.")
    _idempotency_table.update_item(
        Key={"user_id": str(request["user_id"]), "scope_key": str(request["scope_key"])},
        UpdateExpression=(
            "SET #status = :declined, result_message = :message, "
            "updated_at = :now, slot_reserved = :false"
        ),
        ConditionExpression=(
            "(#status = :pending OR #status = :processing) AND "
            "(attribute_not_exists(slot_reserved) OR slot_reserved = :false)"
        ),
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":declined": "DECLINED",
            ":pending": "PENDING",
            ":processing": "PROCESSING",
            ":message": message,
            ":now": int(time.time()),
            ":false": False,
        },
    )


def _mark_declined_after_slot_expiry(
    request: dict[str, Any],
    message: str,
) -> None:
    if not _idempotency_table:
        raise RuntimeError("Idempotency table is not configured.")
    _idempotency_table.update_item(
        Key={"user_id": str(request["user_id"]), "scope_key": str(request["scope_key"])},
        UpdateExpression=(
            "SET #status = :declined, result_message = :message, "
            "slot_reserved = :false, updated_at = :now"
        ),
        ConditionExpression=(
            "(#status = :pending OR #status = :processing) AND slot_reserved = :true"
        ),
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":declined": "DECLINED",
            ":pending": "PENDING",
            ":processing": "PROCESSING",
            ":message": message,
            ":false": False,
            ":true": True,
            ":now": int(time.time()),
        },
    )


def _release_reserved_slot_and_decline(
    request: dict[str, Any],
    message: str,
) -> None:
    if not _slots_table or not _idempotency_table:
        raise RuntimeError("Booking tables are not configured.")
    _dynamodb_client.transact_write_items(
        TransactItems=[
            {
                "Update": {
                    "TableName": SLOTS_TABLE_NAME,
                    "Key": _serialize_values(
                        {
                            "doctor_id": str(request["doctor_id"]),
                            "slot_sk": str(request["slot_sk"]),
                        }
                    ),
                    "UpdateExpression": "SET booked_count = booked_count - :one",
                    "ConditionExpression": "booked_count > :zero",
                    "ExpressionAttributeValues": _serialize_values(
                        {":one": 1, ":zero": 0}
                    ),
                }
            },
            {
                "Update": {
                    "TableName": IDEMPOTENCY_TABLE_NAME,
                    "Key": _serialize_values(
                        {
                            "user_id": str(request["user_id"]),
                            "scope_key": str(request["scope_key"]),
                        }
                    ),
                    "UpdateExpression": (
                        "SET #status = :declined, result_message = :message, "
                        "slot_reserved = :false, updated_at = :now"
                    ),
                    "ConditionExpression": "#status = :processing AND slot_reserved = :true",
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": _serialize_values(
                        {
                            ":declined": "DECLINED",
                            ":processing": "PROCESSING",
                            ":message": message,
                            ":false": False,
                            ":true": True,
                            ":now": int(time.time()),
                        }
                    ),
                }
            },
        ]
    )


def _appointment_ids(request: dict[str, Any]) -> tuple[str, str]:
    stable_id = str(request["booking_id"])
    appointment_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"appointment:{stable_id}"))
    payment_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"payment:{stable_id}"))
    return appointment_id, payment_id


def _create_or_recover_appointment(
    request: dict[str, Any],
    appointment_id: str,
    payment_id: str,
) -> tuple[bool, dict[str, Any]]:
    notification: dict[str, Any] = {}
    with database_transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT appointment_id, appointment_status
                FROM appointments
                WHERE appointment_id = %s
                LIMIT 1
                """,
                (appointment_id,),
            )
            existing = cursor.fetchone()
            if not existing:
                cursor.execute(
                    """
                    SELECT appointment_id
                    FROM appointments
                    WHERE patient_id = %s
                      AND consultation_type_id = %s
                      AND appointment_datetime = %s
                      AND appointment_status NOT IN (
                          'CANCELLED', 'CANCELLED_BY_PATIENT', 'CANCELLED_BY_DOCTOR'
                      )
                    LIMIT 1
                    """,
                    (
                        request["patient_id"],
                        request["consultation_type_id"],
                        _mysql_datetime(request["appointment_datetime"]),
                    ),
                )
                if cursor.fetchone():
                    raise ExistingAppointmentError(
                        "You already have an appointment for this slot."
                    )

                cursor.execute(
                    """
                    SELECT ct.fee, ct.is_active, d.is_verified
                    FROM consultation_types ct
                    JOIN doctors d ON d.doctor_id = ct.doctor_id
                    WHERE ct.consultation_type_id = %s AND ct.doctor_id = %s
                    LIMIT 1
                    """,
                    (request["consultation_type_id"], request["doctor_id"]),
                )
                consultation = cursor.fetchone()
                if (
                    not consultation
                    or not consultation["is_active"]
                    or not consultation["is_verified"]
                ):
                    raise ExistingAppointmentError(
                        "The consultation type is no longer bookable."
                    )

                cursor.execute(
                    """
                    INSERT INTO appointments (
                        appointment_id, patient_id, consultation_type_id,
                        appointment_datetime, appointment_status
                    ) VALUES (%s, %s, %s, %s, 'CONFIRMED')
                    """,
                    (
                        appointment_id,
                        request["patient_id"],
                        request["consultation_type_id"],
                        _mysql_datetime(request["appointment_datetime"]),
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO payments (
                        payment_id, appointment_id, amount, payment_status, payment_method
                    ) VALUES (%s, %s, %s, 'INITIATED', 'PAY_AT_CLINIC')
                    """,
                    (payment_id, appointment_id, consultation["fee"]),
                )
            else:
                cursor.execute(
                    """
                    SELECT fee
                    FROM consultation_types
                    WHERE consultation_type_id = %s
                    LIMIT 1
                    """,
                    (request["consultation_type_id"],),
                )
                consultation = cursor.fetchone()
                if not consultation:
                    raise RuntimeError("Booking consultation type no longer exists.")
                cursor.execute(
                    """
                    INSERT INTO payments (
                        payment_id, appointment_id, amount, payment_status, payment_method
                    ) VALUES (%s, %s, %s, 'INITIATED', 'PAY_AT_CLINIC')
                    ON DUPLICATE KEY UPDATE payment_id = payment_id
                    """,
                    (payment_id, appointment_id, consultation["fee"]),
                )

            notification = {
                "appointment_id": appointment_id,
                "patient_id": str(request["patient_id"]),
                "consultation_type_id": str(request["consultation_type_id"]),
                "appointment_datetime": _utc_datetime(request["appointment_datetime"]),
            }
    owns_slot = not existing or existing["appointment_status"] not in {
        "CANCELLED",
        "CANCELLED_BY_PATIENT",
        "CANCELLED_BY_DOCTOR",
    }
    return owns_slot, notification


def _mysql_datetime(value: Any) -> str:
    return _utc_datetime(value).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _utc_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _mark_confirmed(
    request: dict[str, Any],
    appointment_id: str,
    owns_slot: bool,
) -> None:
    if not _idempotency_table:
        raise RuntimeError("Idempotency table is not configured.")
    _idempotency_table.update_item(
        Key={"user_id": str(request["user_id"]), "scope_key": str(request["scope_key"])},
        UpdateExpression=(
            "SET #status = :confirmed, appointment_id = :appointment, "
            "result_message = :message, slot_reserved = :reserved, updated_at = :now"
        ),
        ConditionExpression=(
            "(#status = :pending OR #status = :processing) AND slot_reserved = :true"
        ),
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":confirmed": "CONFIRMED",
            ":pending": "PENDING",
            ":processing": "PROCESSING",
            ":appointment": appointment_id,
            ":message": "Appointment confirmed.",
            ":reserved": owns_slot,
            ":now": int(time.time()),
        },
    )


def _process_booking(user_id: str, scope_key: str) -> dict[str, Any]:
    request = _get_request(user_id, scope_key)
    if not request:
        return {"status": "EXPIRED", "notification": None}
    request["user_id"] = user_id
    request["scope_key"] = scope_key
    status = str(request.get("status", "PENDING"))
    if status in TERMINAL_STATUSES:
        return {"status": status, "notification": None}
    if status not in {"PENDING", "PROCESSING"}:
        raise RuntimeError("Booking request has an unsupported status.")

    requested_start = _utc_datetime(request["appointment_datetime"])
    slot_start_epoch = int(requested_start.timestamp())
    if str(request["slot_sk"]) != (
        f"{slot_start_epoch}#{request['consultation_type_id']}"
    ):
        message = "The booking request refers to an invalid slot."
        if request.get("slot_reserved"):
            _release_reserved_slot_and_decline(request, message)
        else:
            _mark_declined_without_reservation(request, message)
        return {"status": "DECLINED", "notification": None}

    appointment_id, payment_id = _appointment_ids(request)
    try:
        try:
            slot = _reserve_slot(request)
        except BookingAlreadyHandled as exc:
            return {"status": str(exc), "notification": None}
        if (
            str(slot.get("consultation_type_id")) != str(request["consultation_type_id"])
            or _utc_datetime(slot["start_time"]) != requested_start
            or requested_start <= datetime.now(timezone.utc)
            or int(slot.get("expires_at", 0) or 0) <= int(time.time())
        ):
            raise ExistingAppointmentError(
                "The selected slot is inactive, invalid, or has passed."
            )
        owns_slot, notification = _create_or_recover_appointment(
            request,
            appointment_id,
            payment_id,
        )
    except ExistingAppointmentError as exc:
        if request.get("slot_reserved"):
            if _get_slot(str(request["doctor_id"]), str(request["slot_sk"])):
                _release_reserved_slot_and_decline(request, str(exc))
            else:
                _mark_declined_after_slot_expiry(request, str(exc))
        else:
            _mark_declined_without_reservation(request, str(exc))
        return {"status": "DECLINED", "notification": None}

    _mark_confirmed(request, appointment_id, owns_slot)
    return {
        "status": "CONFIRMED",
        "appointment_id": appointment_id,
        "notification": notification,
    }


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    failures: list[dict[str, str]] = []
    for record in event.get("Records", []):
        message_id = str(record.get("messageId") or "")
        try:
            user_id, scope_key = _request_from_message(record)
            result = _process_booking(user_id, scope_key)
            notification = result.get("notification")
            if notification:
                try:
                    publish_appointment_notification(
                        appointment_id=notification["appointment_id"],
                        event_type="APPOINTMENT_BOOKED",
                        patient_id=notification["patient_id"],
                        consultation_type_id=notification["consultation_type_id"],
                        appointment_datetime=notification["appointment_datetime"],
                        message="Your appointment booking is confirmed.",
                    )
                except Exception:
                    logger.exception("Booking committed but notification delivery failed.")
        except Exception:
            logger.exception("Booking worker failed to process a queue message.")
            if message_id:
                failures.append({"itemIdentifier": message_id})
    return {"batchItemFailures": failures}
