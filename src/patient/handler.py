"""Patient dashboard and booking API."""

import base64
import hashlib
import json
import logging
import os
import re
import time
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError
from shared.booking import calculate_refund, format_money, load_refund_policy
from shared.database import database_transaction, execute_query, execute_write
from shared.notifications import publish_appointment_notification
from read_api.service import (
    convert_booked_seat_to_hold,
    get_slot_by_id,
    release_slot_seat,
    convert_held_seat_to_booked,
    slot_availability_state,
    slot_id_for_datetime,
)


logger = logging.getLogger(__name__)
BOOKING_QUEUE_URL = os.environ.get("BOOKING_QUEUE_URL", "").strip()
IDEMPOTENCY_TABLE_NAME = os.environ.get("IDEMPOTENCY_TABLE", "").strip()
WAITLIST_OFFER_TABLE_NAME = os.environ.get("WAITLIST_OFFER_TABLE", "").strip()
_dynamodb = boto3.resource(
    "dynamodb",
    region_name=os.environ.get("AWS_REGION", "ap-south-1"),
)
_idempotency_table = (
    _dynamodb.Table(IDEMPOTENCY_TABLE_NAME) if IDEMPOTENCY_TABLE_NAME else None
)
_waitlist_offer_table = (
    _dynamodb.Table(WAITLIST_OFFER_TABLE_NAME) if WAITLIST_OFFER_TABLE_NAME else None
)
BOOKING_QUEUE = boto3.client(
    "sqs",
    region_name=os.environ.get("AWS_REGION", "ap-south-1"),
)


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat() + ("Z" if value.tzinfo is None else "")
    if isinstance(value, date):
        return value.isoformat()
    return value


def _response(status_code: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
        },
        "body": json.dumps(payload, default=_json_value),
    }


def _get_request_body(event: dict[str, Any]) -> dict[str, Any]:
    body = event.get("body")
    if isinstance(body, dict):
        return body
    if not body:
        return {}

    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")

    try:
        parsed = json.loads(body)
    except (TypeError, ValueError, UnicodeDecodeError):
        return {}

    return parsed if isinstance(parsed, dict) else {}


def _request_header(event: dict[str, Any], name: str) -> str:
    headers = event.get("headers") or {}
    target = name.lower()
    return next(
        (str(value).strip() for key, value in headers.items() if str(key).lower() == target),
        "",
    )


def _submit_booking_request(
    patient_id: str,
    user_id: str,
    consultation_type_id: str,
    appointment_dt_utc: datetime,
    idempotency_key: str,
) -> tuple[int, dict[str, Any]]:
    if not BOOKING_QUEUE_URL or not _idempotency_table:
        logger.error("Booking queue or idempotency table is not configured.")
        return 503, {"message": "Booking is temporarily unavailable."}

    canonical_request = json.dumps(
        {
            "consultation_type_id": consultation_type_id,
            "appointment_datetime": appointment_dt_utc.isoformat(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    request_hash = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
    scope_key = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    now = int(time.time())
    existing = _idempotency_table.get_item(
        Key={"user_id": user_id, "scope_key": scope_key},
        ConsistentRead=True,
    ).get("Item")

    if existing and int(existing.get("expires_at", 0)) > now:
        if existing.get("request_hash") != request_hash:
            return 409, {
                "message": "This idempotency key was already used for a different booking request."
            }
    else:
        consultation_rows = execute_query(
            """
            SELECT ct.consultation_type_id, ct.doctor_id, ct.is_active,
                   d.is_verified
            FROM consultation_types ct
            JOIN doctors d ON d.doctor_id = ct.doctor_id
            WHERE ct.consultation_type_id = %s
            LIMIT 1
            """,
            (consultation_type_id,),
        )
        if not consultation_rows:
            return 404, {"message": "Consultation type not found."}
        consultation = consultation_rows[0]
        if not consultation.get("is_active") or not consultation.get("is_verified"):
            return 409, {"message": "This consultation type is not currently bookable."}

        slot_id = slot_id_for_datetime(consultation_type_id, appointment_dt_utc)
        slot = get_slot_by_id(slot_id)
        if (
            not slot
            or str(slot.get("doctor_id")) != str(consultation["doctor_id"])
            or slot_availability_state(slot) in {"missing", "inactive", "past", "no_capacity"}
        ):
            return 409, {"message": "Selected consultation slot is not currently available."}

        request_item = {
            "user_id": user_id,
            "scope_key": scope_key,
            "request_id": scope_key,
            "booking_id": str(uuid.uuid4()),
            "request_hash": request_hash,
            "patient_id": patient_id,
            "doctor_id": str(consultation["doctor_id"]),
            "slot_id": slot_id,
            "slot_sk": str(slot["slot_sk"]),
            "consultation_type_id": consultation_type_id,
            "appointment_datetime": appointment_dt_utc.isoformat().replace("+00:00", "Z"),
            "status": "PENDING",
            "slot_reserved": False,
            "expires_at": now + 30 * 86400,
            "created_at": now,
            "updated_at": now,
        }

        try:
            _idempotency_table.put_item(
                Item=request_item,
                ConditionExpression="attribute_not_exists(user_id) OR expires_at <= :now",
                ExpressionAttributeValues={":now": now},
            )
            existing = request_item
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            existing = _idempotency_table.get_item(
                Key={"user_id": user_id, "scope_key": scope_key},
                ConsistentRead=True,
            ).get("Item")
            if not existing:
                raise RuntimeError("Could not read the existing idempotency record.")
            if int(existing.get("expires_at", 0)) <= now:
                _idempotency_table.put_item(
                    Item=request_item,
                    ConditionExpression="expires_at <= :now",
                    ExpressionAttributeValues={":now": now},
                )
                existing = request_item
            elif existing.get("request_hash") != request_hash:
                return 409, {
                    "message": "This idempotency key was already used for a different booking request."
                }

    request_id = str(existing["request_id"])
    status = str(existing.get("status", "PENDING"))
    if status == "CONFIRMED":
        return 200, {
            "request_id": request_id,
            "status": status,
            "appointment_id": existing.get("appointment_id"),
            "message": existing.get("result_message") or "Appointment confirmed.",
        }
    if status == "DECLINED":
        return 409, {
            "request_id": request_id,
            "status": status,
            "message": existing.get("result_message") or "Booking request was declined.",
        }
    if status == "FAILED":
        _idempotency_table.update_item(
            Key={"user_id": user_id, "scope_key": scope_key},
            UpdateExpression="SET #status = :pending, updated_at = :now",
            ConditionExpression="#status = :failed",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":pending": "PENDING",
                ":failed": "FAILED",
                ":now": now,
            },
        )

    try:
        BOOKING_QUEUE.send_message(
            QueueUrl=BOOKING_QUEUE_URL,
            MessageBody=json.dumps(
                {"user_id": user_id, "scope_key": scope_key}
            ),
            MessageGroupId=f"{existing['doctor_id']}#{existing['slot_sk']}",
            MessageDeduplicationId=str(existing["booking_id"]),
        )
    except BotoCoreError:
        logger.exception("Could not enqueue booking request.")
        return 503, {"message": "Booking is temporarily unavailable. Please retry."}

    return (
        202,
        {
            "request_id": request_id,
            "status": "PENDING",
            "message": "Booking request received and is being processed.",
            "status_url": f"/patients/me/booking-requests/{request_id}",
        },
    )


def _coerce_utc_datetime(raw_value: Any) -> datetime | None:
    if raw_value in (None, ""):
        return None
    if isinstance(raw_value, datetime):
        value = raw_value
    else:
        value = str(raw_value)
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        value = value.replace(" ", "T")
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _list_doctors(query_params: dict[str, str]) -> dict[str, Any]:
    search_term = (query_params.get("search") or query_params.get("q") or "").strip()
    specialization = (
        query_params.get("specialization")
        or query_params.get("specialisation")
        or ""
    ).strip()
    hospital = (query_params.get("hospital") or "").strip()
    city = (query_params.get("city") or "").strip()
    limit = 20
    page = 1
    try:
        limit = max(1, min(int(query_params.get("limit", "20")), 50))
    except ValueError:
        pass
    try:
        page = max(1, int(query_params.get("page", "1")))
    except ValueError:
        pass

    sql = """
        SELECT d.doctor_id,
               CONCAT_WS(' ', d.first_name, d.last_name) AS doctor_name,
               d.experience_years,
               d.timezone,
               h.name AS hospital_name,
               h.address AS hospital_address,
               TRIM(SUBSTRING_INDEX(SUBSTRING_INDEX(h.address, ',', -2), ',', 1)) AS city,
               GROUP_CONCAT(DISTINCT s.name ORDER BY s.name SEPARATOR ', ') AS specializations,
               MIN(CASE WHEN ct.is_active = TRUE THEN ct.fee ELSE NULL END) AS starting_fee,
               COUNT(*) OVER() AS total_count
        FROM doctors d
        JOIN hospitals h ON h.hospital_id = d.hospital_id
        LEFT JOIN doctor_specializations ds ON ds.doctor_id = d.doctor_id
        LEFT JOIN specializations s ON s.specialization_id = ds.specialization_id
        JOIN consultation_types ct ON ct.doctor_id = d.doctor_id AND ct.is_active = TRUE
        WHERE d.is_verified = TRUE
    """
    params: list[Any] = []

    if search_term:
        sql += " AND (LOWER(CONCAT_WS(' ', d.first_name, d.last_name)) LIKE LOWER(%s) OR LOWER(h.name) LIKE LOWER(%s)) "
        term = f"%{search_term}%"
        params.extend([term, term])

    if specialization:
        sql += " AND s.name = %s "
        params.append(specialization)

    if hospital:
        sql += " AND h.hospital_id = %s "
        params.append(hospital)

    if city:
        sql += " AND h.address LIKE %s "
        params.append(f"%{city}%")

    sql += " GROUP BY d.doctor_id, d.first_name, d.last_name, d.experience_years, d.timezone, h.name, h.address "
    term_pattern = f"%{search_term}%"
    sql += """
        ORDER BY
            CASE
                WHEN %s = '' THEN 0
                WHEN LOWER(CONCAT_WS(' ', d.first_name, d.last_name)) = LOWER(%s) THEN 0
                WHEN LOWER(CONCAT_WS(' ', d.first_name, d.last_name)) LIKE LOWER(%s) THEN 1
                WHEN LOWER(h.name) = LOWER(%s) THEN 2
                WHEN LOWER(h.name) LIKE LOWER(%s) THEN 3
                ELSE 4
            END ASC,
            d.experience_years DESC,
            starting_fee ASC,
            d.last_name ASC,
            d.first_name ASC
    """
    params.extend([search_term, search_term, term_pattern, search_term, term_pattern])
    sql += " LIMIT %s OFFSET %s "
    params.extend([limit, (page - 1) * limit])

    rows = execute_query(sql, tuple(params))
    total = int(rows[0].get("total_count", 0)) if rows else 0
    doctors = []
    for row in rows:
        doctor = dict(row)
        doctor.pop("total_count", None)
        doctors.append(doctor)
    return {"doctors": doctors, "total": total, "page": page, "limit": limit}


def _doctor_search_options() -> dict[str, Any]:
    hospitals = execute_query(
        """
        SELECT h.hospital_id, h.name,
               TRIM(SUBSTRING_INDEX(SUBSTRING_INDEX(h.address, ',', -2), ',', 1)) AS city
        FROM hospitals h
        JOIN doctors d ON d.hospital_id = h.hospital_id
        WHERE d.is_verified = TRUE
        GROUP BY h.hospital_id, h.name, h.address
        ORDER BY h.name ASC
        """
    )
    specializations = execute_query(
        """
        SELECT DISTINCT s.name
        FROM specializations s
        JOIN doctor_specializations ds ON ds.specialization_id = s.specialization_id
        JOIN doctors d ON d.doctor_id = ds.doctor_id
        WHERE d.is_verified = TRUE
        ORDER BY s.name ASC
        """
    )
    cities = sorted({
        str(row.get("city") or "").strip()
        for row in hospitals
        if str(row.get("city") or "").strip()
    })
    return {
        "hospitals": hospitals,
        "specializations": [row["name"] for row in specializations],
        "cities": cities,
    }


def _get_patient_id(authorizer: dict[str, Any]) -> str | None:
    user_id = str(authorizer.get("userId") or authorizer.get("sub") or "").strip()
    if not user_id:
        return None
    rows = execute_query(
        "SELECT patient_id FROM patients WHERE user_id = %s LIMIT 1",
        (user_id,),
    )
    if rows:
        return str(rows[0]["patient_id"])

    rows = execute_query(
        "SELECT patient_id FROM patients WHERE patient_id = %s LIMIT 1",
        (user_id,),
    )
    return str(rows[0]["patient_id"]) if rows else None


def _get_patient_profile(patient_id: str) -> dict[str, Any] | None:
    rows = execute_query(
        """
        SELECT p.patient_id, p.first_name, p.last_name, p.phone_number,
               p.date_of_birth, p.gender, p.emergency_contact_name,
               p.emergency_contact_phone, p.profile_image_key,
               u.email, u.is_active
        FROM patients p
        JOIN users u ON u.user_id = p.user_id
        WHERE p.patient_id = %s
        LIMIT 1
        """,
        (patient_id,),
    )
    if not rows:
        return None

    profile = dict(rows[0])
    profile["id"] = str(profile.pop("patient_id"))
    profile["full_name"] = " ".join(
        part for part in (profile.get("first_name"), profile.get("last_name")) if part
    )
    if isinstance(profile.get("date_of_birth"), date):
        profile["date_of_birth"] = profile["date_of_birth"].isoformat()
    return profile


def _update_patient_profile(patient_id: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    allowed_fields = {
        "first_name": ("first_name", 100),
        "last_name": ("last_name", 100),
        "phone_number": ("phone_number", 20),
        "emergency_contact_name": ("emergency_contact_name", 150),
        "emergency_contact_phone": ("emergency_contact_phone", 20),
    }
    updates: list[str] = []
    params: list[Any] = []

    for payload_key, (column, max_length) in allowed_fields.items():
        if payload_key not in payload:
            continue
        value = str(payload[payload_key] or "").strip()
        if len(value) > max_length:
            return 400, {"message": f"{payload_key} is too long."}
        updates.append(f"{column} = %s")
        params.append(value or None)

    if "date_of_birth" in payload:
        raw_value = str(payload["date_of_birth"] or "").strip()
        try:
            date_of_birth = date.fromisoformat(raw_value)
        except ValueError:
            return 400, {"message": "date_of_birth must use YYYY-MM-DD format."}
        updates.append("date_of_birth = %s")
        params.append(date_of_birth)

    if "gender" in payload:
        gender = str(payload["gender"] or "").strip().upper()
        if gender not in {"MALE", "FEMALE", "OTHER"}:
            return 400, {"message": "gender must be MALE, FEMALE, or OTHER."}
        updates.append("gender = %s")
        params.append(gender)

    if not updates:
        return 400, {"message": "No supported profile fields were provided."}

    params.append(patient_id)
    execute_write(
        f"UPDATE patients SET {', '.join(updates)} WHERE patient_id = %s",
        tuple(params),
    )
    profile = _get_patient_profile(patient_id)
    return (200 if profile else 404), ({"patient": profile} if profile else {"message": "Patient profile not found."})


def _deactivate_patient(patient_id: str) -> tuple[int, dict[str, Any]]:
    rows = execute_query(
        "SELECT user_id FROM patients WHERE patient_id = %s LIMIT 1",
        (patient_id,),
    )
    if not rows:
        return 404, {"message": "Patient profile not found."}
    execute_write(
        "UPDATE users SET is_active = FALSE WHERE user_id = %s",
        (rows[0]["user_id"],),
    )
    return 200, {"message": "Patient account has been deactivated."}


def _refund_quote(appointment: dict[str, Any], cancellation_by: str) -> dict[str, Any]:
    return calculate_refund(
        appointment["fee_amount"],
        appointment["appointment_datetime"],
        datetime.now(timezone.utc),
        cancellation_by=cancellation_by,
        policy=load_refund_policy(),
    )


def _sweep_expired_waitlist_offers() -> int:
    if not _waitlist_offer_table:
        raise RuntimeError("Waitlist offer table is not configured for legacy offer cleanup.")

    now = int(time.time())
    query_args: dict[str, Any] = {
        "IndexName": "gsi_live",
        "KeyConditionExpression": (
            Key("live_shard").eq("ACTIVE") & Key("expires_at").lte(now)
        ),
    }
    expired: list[dict[str, Any]] = []
    while True:
        page = _waitlist_offer_table.query(**query_args)
        expired.extend(page.get("Items", []))
        last_key = page.get("LastEvaluatedKey")
        if not last_key:
            break
        query_args["ExclusiveStartKey"] = last_key

    processed = 0
    for offer in expired:
        try:
            _waitlist_offer_table.update_item(
                Key={"slot_ref": offer["slot_ref"], "patient_id": offer["patient_id"]},
                UpdateExpression="SET #status = :expired REMOVE live_shard",
                ConditionExpression=(
                    "#status IN (:open, :accepting) AND expires_at <= :now"
                ),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":expired": "EXPIRED",
                    ":open": "OPEN",
                    ":accepting": "ACCEPTING",
                    ":now": now,
                },
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                continue
            raise

        slot = get_slot_by_id(str(offer["slot_ref"]))
        if not slot:
            logger.error("Expired offer %s references missing slot.", offer.get("offer_id"))
            continue
        release_slot_seat(slot, held=True)
        processed += 1
    return processed


def _cancel_patient_appointment(
    patient_id: str,
    appointment_id: str,
) -> tuple[int, dict[str, Any]]:
    rows = execute_query(
        """
        SELECT a.appointment_id, a.patient_id, a.consultation_type_id,
               a.appointment_datetime, a.appointment_status,
               COALESCE(pay.amount, ct.fee) AS fee_amount, pay.payment_id
        FROM appointments a
        JOIN consultation_types ct ON ct.consultation_type_id = a.consultation_type_id
        LEFT JOIN payments pay ON pay.payment_id = (
            SELECT p.payment_id FROM payments p
            WHERE p.appointment_id = a.appointment_id
            ORDER BY p.created_at DESC LIMIT 1
        )
        WHERE a.appointment_id = %s AND a.patient_id = %s
        LIMIT 1
        """,
        (appointment_id, patient_id),
    )
    if not rows:
        return 404, {"message": "Appointment not found."}
    appointment = rows[0]
    if appointment["appointment_status"] != "CONFIRMED":
        return 409, {"message": "Only confirmed appointments can be cancelled."}

    quote = _refund_quote(appointment, "patient")
    slot_id = slot_id_for_datetime(
        str(appointment["consultation_type_id"]),
        appointment["appointment_datetime"].replace(tzinfo=timezone.utc),
    )
    slot = get_slot_by_id(slot_id)
    if not slot:
        logger.error("Cannot cancel appointment %s because its slot is missing.", appointment_id)
        return 409, {"message": "Appointment slot is unavailable for cancellation."}

    seat_held = False
    try:
        convert_booked_seat_to_hold(slot)
        seat_held = True
        with database_transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE appointments
                    SET appointment_status = 'CANCELLED_BY_PATIENT'
                    WHERE appointment_id = %s
                      AND patient_id = %s
                      AND appointment_status = 'CONFIRMED'
                    """,
                    (appointment_id, patient_id),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("Appointment was changed by another request.")
                if appointment.get("payment_id"):
                    cursor.execute(
                        """
                        INSERT INTO refunds (
                            refund_id, payment_id, refund_amount, refund_status
                        ) VALUES (%s, %s, %s, 'PENDING')
                        """,
                        (
                            str(uuid.uuid4()),
                            appointment["payment_id"],
                            quote["refund_amount"],
                        ),
                    )
    except Exception:
        logger.exception("Patient cancellation failed for appointment %s.", appointment_id)
        if seat_held:
            try:
                convert_held_seat_to_booked(slot)
            except Exception:
                logger.exception("Patient cancellation seat compensation failed.")
        return 500, {"message": "Cancellation could not be completed. Please retry or contact support."}

    try:
        release_slot_seat(slot, held=True)
    except Exception:
        logger.exception("Appointment %s was cancelled but its slot hold could not be released.", appointment_id)
    message = (
        f"Appointment {appointment_id} was cancelled. "
        f"Refund ledger amount: INR {format_money(quote['refund_amount'])}."
    )
    try:
        publish_appointment_notification(
            appointment_id=appointment_id,
            event_type="APPOINTMENT_CANCELLED_BY_PATIENT",
            patient_id=patient_id,
            consultation_type_id=str(appointment["consultation_type_id"]),
            appointment_datetime=appointment["appointment_datetime"].replace(tzinfo=timezone.utc),
            message=message,
        )
    except Exception:
        logger.exception("Appointment %s was cancelled but notification failed.", appointment_id)
    return 200, {
        "appointment_id": appointment_id,
        "status": "CANCELLED_BY_PATIENT",
        "refund_amount": quote["refund_amount"],
        "refund_percent": quote["refund_percent"],
        "refund_tier": quote["tier"],
    }


def _refund_preview(patient_id: str, appointment_id: str) -> tuple[int, dict[str, Any]]:
    rows = execute_query(
        """
        SELECT a.appointment_datetime, a.appointment_status,
               COALESCE(
                   (SELECT p.amount FROM payments p
                    WHERE p.appointment_id = a.appointment_id
                    ORDER BY p.created_at DESC LIMIT 1),
                   ct.fee
               ) AS fee_amount
        FROM appointments a
        JOIN consultation_types ct ON ct.consultation_type_id = a.consultation_type_id
        WHERE a.appointment_id = %s AND a.patient_id = %s
        LIMIT 1
        """,
        (appointment_id, patient_id),
    )
    if not rows:
        return 404, {"message": "Appointment not found."}
    appointment = rows[0]
    if appointment["appointment_status"] != "CONFIRMED":
        return 409, {"message": "Refund preview is available only for confirmed appointments."}
    quote = _refund_quote(appointment, "patient")
    return 200, {
        "appointment_id": appointment_id,
        "refund_amount": quote["refund_amount"],
        "refund_percent": quote["refund_percent"],
        "refund_tier": quote["tier"],
        "currency": "INR",
        "payment_method": "PAY_AT_CLINIC",
        "note": "Refund is a ledger estimate only; MediSlot does not process payments.",
    }


def lambda_handler(event: dict[str, Any] | None, context: Any = None) -> dict[str, Any]:
    event = event if isinstance(event, dict) else {}
    if event.get("source") == "aws.events":
        processed = _sweep_expired_waitlist_offers()
        return _response(200, {"expired_offers_processed": processed})

    request_context = event.get("requestContext") or {}
    method = (
        event.get("httpMethod")
        or (request_context.get("http") or {}).get("method")
        or ""
    ).upper()
    path = (
        event.get("path")
        or event.get("rawPath")
        or (request_context.get("http") or {}).get("path")
        or ""
    ).rstrip("/").lower()
    path_parts = [part for part in path.split("/") if part]
    if path_parts and path_parts[0] in {"dev", "test", "prod", "v1", "v2"}:
        path_parts = path_parts[1:]
    path = "/" + "/".join(path_parts)

    authorizer = request_context.get("authorizer") or {}
    if str(authorizer.get("role", "")).lower() != "patient":
        return _response(403, {"message": "Patient role required."})

    patient_id = _get_patient_id(authorizer)
    if not patient_id:
        return _response(401, {"message": "Authentication required."})

    if method == "GET" and path == "/patients/me":
        profile = _get_patient_profile(patient_id)
        if not profile:
            return _response(404, {"message": "Patient profile not found."})
        return _response(200, {"patient": profile})

    if method == "PATCH" and path == "/patients/me":
        status_code, response = _update_patient_profile(
            patient_id,
            _get_request_body(event),
        )
        return _response(status_code, response)

    if method == "DELETE" and path == "/patients/me":
        status_code, response = _deactivate_patient(patient_id)
        return _response(status_code, response)

    if method == "GET" and path.endswith("/patients/me/dashboard"):
        patient_rows = execute_query(
            """
             SELECT p.patient_id AS id,
                 CONCAT_WS(' ', p.first_name, p.last_name) AS full_name,
                 u.email
             FROM patients p
             JOIN users u ON u.user_id = p.user_id
             WHERE p.patient_id = %s AND u.is_active = TRUE
            """,
            (patient_id,),
        )
        if not patient_rows:
            return _response(404, {"message": "Patient profile not found."})

        upcoming_rows = execute_query(
            """
            SELECT COUNT(*) AS count
                    FROM appointments
                    WHERE patient_id = %s
                        AND appointment_status = 'CONFIRMED'
                        AND appointment_datetime >= UTC_TIMESTAMP()
            """,
            (patient_id,),
        )
        appointments = execute_query(
            """
            SELECT
                a.appointment_id AS id,
                a.appointment_datetime AS slot_start_utc,
                a.appointment_status AS status,
                COALESCE(
                    (SELECT pay.amount FROM payments pay
                     WHERE pay.appointment_id = a.appointment_id
                     ORDER BY pay.created_at DESC LIMIT 1),
                    ct.fee
                ) AS fee_amount,
                (SELECT pay.payment_status FROM payments pay
                 WHERE pay.appointment_id = a.appointment_id
                 ORDER BY pay.created_at DESC LIMIT 1) AS payment_status,
                (SELECT pay.payment_method FROM payments pay
                 WHERE pay.appointment_id = a.appointment_id
                 ORDER BY pay.created_at DESC LIMIT 1) AS payment_method,
                COALESCE(
                    (SELECT SUM(r.refund_amount) FROM refunds r
                     JOIN payments rp ON rp.payment_id = r.payment_id
                     WHERE rp.appointment_id = a.appointment_id),
                    0
                ) AS refund_amount,
                catalog.name AS consultation_type,
                CONCAT_WS(' ', d.first_name, d.last_name) AS doctor_name,
                h.name AS hospital_name,
                h.address AS hospital_city
            FROM appointments a
            JOIN consultation_types ct ON ct.consultation_type_id = a.consultation_type_id
            JOIN consultation_catalog catalog ON catalog.consultation_type_id = ct.catalog_type_id
            JOIN doctors d ON d.doctor_id = ct.doctor_id
            JOIN hospitals h ON h.hospital_id = d.hospital_id
            WHERE a.patient_id = %s
            ORDER BY
                CASE
                    WHEN a.appointment_status = 'CONFIRMED' AND a.appointment_datetime >= UTC_TIMESTAMP() THEN 0
                    ELSE 1
                END,
                a.appointment_datetime ASC
            LIMIT 50
            """,
            (patient_id,),
        )

        return _response(
            200,
            {
                "patient": patient_rows[0],
                "upcoming_appointments_count": int(upcoming_rows[0]["count"]),
                "appointments": appointments,
            },
        )

    if method == "GET" and path in {"/patients/doctors/search", "/patients/search/doctors"}:
        return _response(200, _list_doctors(event.get("queryStringParameters") or {}))

    if method == "GET" and path == "/patients/doctors/search-options":
        return _response(200, _doctor_search_options())

    if (
        method == "GET"
        and len(path_parts) == 4
        and path_parts[:3] == ["patients", "me", "booking-requests"]
    ):
        if not _idempotency_table:
            logger.error("Idempotency table is not configured.")
            return _response(503, {"message": "Booking status is temporarily unavailable."})
        request = _idempotency_table.get_item(
            Key={
                "user_id": str(authorizer.get("userId") or authorizer.get("sub") or patient_id),
                "scope_key": path_parts[3],
            },
            ConsistentRead=True,
        ).get("Item")
        if not request:
            return _response(404, {"message": "Booking request not found."})
        return _response(
            200,
            {
                "booking_request": {
                    "request_id": request["request_id"],
                    "status": request.get("status", "PENDING"),
                    "appointment_id": request.get("appointment_id"),
                    "result_message": request.get("result_message"),
                    "created_at": request.get("created_at"),
                    "updated_at": request.get("updated_at"),
                }
            },
        )

    if (
        method == "GET"
        and len(path_parts) == 5
        and path_parts[:3] == ["patients", "me", "appointments"]
        and path_parts[4] == "refund-preview"
    ):
        status_code, response = _refund_preview(patient_id, path_parts[3])
        return _response(status_code, response)

    if (
        method == "POST"
        and len(path_parts) == 5
        and path_parts[:3] == ["patients", "me", "appointments"]
        and path_parts[4] == "cancel"
    ):
        status_code, response = _cancel_patient_appointment(patient_id, path_parts[3])
        return _response(status_code, response)

    if method == "POST" and path == "/patients/me/appointments":
        payload = _get_request_body(event)
        consultation_type_id = str(payload.get("consultation_type_id") or "").strip()
        appointment_datetime = payload.get("appointment_datetime") or payload.get("slot_start_utc")

        if not consultation_type_id or not appointment_datetime:
            return _response(400, {"message": "consultation_type_id and appointment_datetime are required."})

        appointment_dt_utc = _coerce_utc_datetime(appointment_datetime)
        if appointment_dt_utc is None:
            return _response(400, {"message": "appointment_datetime must be a valid ISO-8601 datetime."})

        if appointment_dt_utc <= datetime.now(timezone.utc):
            return _response(400, {"message": "Appointments must be scheduled in the future."})

        idempotency_key = _request_header(event, "Idempotency-Key")
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", idempotency_key):
            return _response(400, {"message": "A valid Idempotency-Key header is required."})

        status_code, response = _submit_booking_request(
            patient_id,
            str(authorizer.get("userId") or authorizer.get("sub") or patient_id),
            consultation_type_id,
            appointment_dt_utc,
            idempotency_key,
        )
        return _response(status_code, response)

    return _response(404, {"message": "Patient route not found."})
