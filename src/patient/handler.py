"""Patient dashboard and booking API."""

import base64
import json
import logging
import time
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from shared.booking import calculate_refund, format_money, load_refund_policy
from shared.database import database_transaction, execute_query
from shared.notifications import publish_appointment_notification
from shared.waitlist import (
    notify_waitlist_offer as _notify_waitlist_offer,
    promote_waitlist as _promote_waitlist,
    query_all_items as _query_all_items,
    waitlist_offer_table as _waitlist_offer_table,
    waitlist_table as _waitlist_table,
)
from read_api.service import (
    convert_booked_seat_to_hold,
    convert_held_seat_to_booked,
    convert_held_seat_to_booking,
    get_slot_by_id,
    release_slot_seat,
    reserve_slot_seat,
    slot_availability_state,
    slot_id_for_datetime,
)


logger = logging.getLogger(__name__)


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
    return str(authorizer.get("userId") or authorizer.get("sub") or "").strip() or None


def _patient_booking_response(
    appointment_id: str,
    consultation_type_id: str,
    appointment_datetime_utc: datetime,
    *,
    fee_amount: Any = None,
    payment_status: str = "INITIATED",
) -> dict[str, Any]:
    return {
        "appointment_id": appointment_id,
        "consultation_type_id": consultation_type_id,
        "appointment_datetime": appointment_datetime_utc.isoformat().replace("+00:00", "Z"),
        "status": "CONFIRMED",
        "fee_amount": float(fee_amount) if fee_amount is not None else None,
        "payment_status": payment_status,
        "payment_method": "PAY_AT_CLINIC",
    }


def _refund_quote(appointment: dict[str, Any], cancellation_by: str) -> dict[str, Any]:
    return calculate_refund(
        appointment["fee_amount"],
        appointment["appointment_datetime"],
        datetime.now(timezone.utc),
        cancellation_by=cancellation_by,
        policy=load_refund_policy(),
    )


def _join_waitlist(patient_id: str, slot_id: str) -> tuple[int, dict[str, Any]]:
    slot = get_slot_by_id(slot_id)
    state = slot_availability_state(slot)
    if state == "missing":
        return 404, {"message": "Slot not found."}
    if state == "past":
        return 409, {"message": "Past slots cannot be waitlisted."}
    if state != "full":
        return 409, {"message": "Waitlists are available only when the slot is full."}
    slot_start = datetime.fromisoformat(str(slot["start_time"]).replace("Z", "+00:00"))
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    existing = _waitlist_table().get_item(
        Key={"slot_ref": slot_id, "patient_id": patient_id}
    ).get("Item")
    if existing:
        return 200, {"message": "You are already on this waitlist.", "joined_at": existing["joined_at"]}

    active_offers = _query_all_items(
        _waitlist_offer_table(),
        IndexName="gsi_patient_offer",
        KeyConditionExpression=(
            Key("patient_id").eq(patient_id)
            & Key("expires_at").gte(int(time.time()))
        ),
    )
    if any(
        offer.get("slot_ref") == slot_id
        and offer.get("status") in {"OPEN", "ACCEPTING"}
        for offer in active_offers
    ):
        return 409, {"message": "You already have an active offer for this slot."}

    future_entries = _query_all_items(
        _waitlist_table(),
        IndexName="gsi_patient",
        KeyConditionExpression=Key("patient_id").eq(patient_id) & Key("slot_start").gt(now_iso),
    )
    if len(future_entries) >= 5:
        return 409, {"message": "You can join at most five waitlists at a time."}

    joined_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _waitlist_table().put_item(
        Item={
            "slot_ref": slot_id,
            "patient_id": patient_id,
            "joined_at": joined_at,
            "slot_start": slot_start.isoformat().replace("+00:00", "Z"),
            "expires_at": int(slot_start.timestamp()),
            "consultation_type_id": str(slot["consultation_type_id"]),
        },
        ConditionExpression="attribute_not_exists(slot_ref)",
    )
    return 201, {"message": "You joined the waitlist.", "joined_at": joined_at}


def _patient_waitlists(patient_id: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    waitlists = _query_all_items(
        _waitlist_table(),
        IndexName="gsi_patient",
        KeyConditionExpression=Key("patient_id").eq(patient_id) & Key("slot_start").gt(now),
    )
    offers = _query_all_items(
        _waitlist_offer_table(),
        IndexName="gsi_patient_offer",
        KeyConditionExpression=(
            Key("patient_id").eq(patient_id)
            & Key("expires_at").gte(int(time.time()))
        ),
    )
    return {"waitlists": waitlists, "offers": offers}


def _accept_waitlist_offer(patient_id: str, offer_id: str) -> tuple[int, dict[str, Any]]:
    offers = _query_all_items(
        _waitlist_offer_table(),
        IndexName="gsi_patient_offer",
        KeyConditionExpression=(
            Key("patient_id").eq(patient_id)
            & Key("expires_at").gte(int(time.time()))
        ),
    )
    offer = next((item for item in offers if item.get("offer_id") == offer_id), None)
    if not offer or offer.get("status") != "OPEN":
        return 404, {"message": "Active waitlist offer not found."}

    now = int(time.time())
    try:
        _waitlist_offer_table().update_item(
            Key={"slot_ref": offer["slot_ref"], "patient_id": patient_id},
            UpdateExpression="SET #status = :accepting",
            ConditionExpression="#status = :open AND expires_at > :now",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":accepting": "ACCEPTING",
                ":open": "OPEN",
                ":now": now,
            },
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return 409, {"message": "This offer has expired or has already been accepted."}
        raise

    slot = get_slot_by_id(str(offer["slot_ref"]))
    if not slot:
        _waitlist_offer_table().update_item(
            Key={"slot_ref": offer["slot_ref"], "patient_id": patient_id},
            UpdateExpression="SET #status = :open",
            ConditionExpression="#status = :accepting",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":open": "OPEN", ":accepting": "ACCEPTING"},
        )
        return 404, {"message": "Offered slot no longer exists."}
    slot_state = slot_availability_state(slot)
    if slot_state not in {"available", "full"}:
        release_slot_seat(slot, held=True)
        _waitlist_offer_table().update_item(
            Key={"slot_ref": offer["slot_ref"], "patient_id": patient_id},
            UpdateExpression="SET #status = :expired REMOVE live_shard",
            ConditionExpression="#status = :accepting",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":expired": "EXPIRED", ":accepting": "ACCEPTING"},
        )
        return 409, {"message": "This offered slot is no longer available."}
    consultation_rows = execute_query(
        """
        SELECT ct.fee, ct.capacity, ct.consultation_type_id
        FROM consultation_types ct
        WHERE ct.consultation_type_id = %s AND ct.is_active = TRUE
        LIMIT 1
        """,
        (offer["consultation_type_id"],),
    )
    if not consultation_rows:
        _waitlist_offer_table().update_item(
            Key={"slot_ref": offer["slot_ref"], "patient_id": patient_id},
            UpdateExpression="SET #status = :open",
            ConditionExpression="#status = :accepting",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":open": "OPEN", ":accepting": "ACCEPTING"},
        )
        return 409, {"message": "This consultation type is no longer available."}
    consultation = consultation_rows[0]
    appointment_id = str(uuid.uuid4())
    appointment_datetime = datetime.fromisoformat(str(slot["start_time"]).replace("Z", "+00:00"))
    seat_converted = False
    try:
        convert_held_seat_to_booking(slot, int(consultation["capacity"]))
        seat_converted = True
        with database_transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO appointments (
                        appointment_id, patient_id, consultation_type_id,
                        appointment_datetime, appointment_status
                    ) VALUES (%s, %s, %s, %s, 'CONFIRMED')
                    """,
                    (
                        appointment_id,
                        patient_id,
                        offer["consultation_type_id"],
                        appointment_datetime.strftime("%Y-%m-%d %H:%M:%S"),
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO payments (
                        payment_id, appointment_id, amount, payment_status, payment_method
                    ) VALUES (%s, %s, %s, 'INITIATED', 'PAY_AT_CLINIC')
                    """,
                    (str(uuid.uuid4()), appointment_id, consultation["fee"]),
                )
    except Exception:
        logger.exception("Could not confirm waitlist offer %s.", offer_id)
        if seat_converted:
            try:
                convert_booked_seat_to_hold(slot)
            except Exception:
                logger.exception("Could not restore held seat for waitlist offer %s.", offer_id)
        try:
            _waitlist_offer_table().update_item(
                Key={"slot_ref": offer["slot_ref"], "patient_id": patient_id},
                UpdateExpression="SET #status = :open",
                ConditionExpression="#status = :accepting",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":open": "OPEN", ":accepting": "ACCEPTING"},
            )
        except ClientError:
            logger.exception("Could not restore waitlist offer %s after booking failure.", offer_id)
        raise

    try:
        _waitlist_offer_table().update_item(
            Key={"slot_ref": offer["slot_ref"], "patient_id": patient_id},
            UpdateExpression="SET #status = :accepted, accepted_at = :now REMOVE live_shard",
            ConditionExpression="#status = :accepting",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":accepted": "ACCEPTED",
                ":accepting": "ACCEPTING",
                ":now": now,
            },
        )
    except ClientError:
        logger.exception("Appointment %s was booked but offer %s could not be finalized.", appointment_id, offer_id)
    try:
        publish_appointment_notification(
            appointment_id=appointment_id,
            event_type="APPOINTMENT_BOOKED",
            patient_id=patient_id,
            consultation_type_id=str(offer["consultation_type_id"]),
            appointment_datetime=appointment_datetime,
            message="Your waitlist offer was accepted and the appointment is confirmed.",
        )
    except Exception:
        logger.exception("Waitlist appointment %s was booked but notification failed.", appointment_id)
    return 201, _patient_booking_response(
        appointment_id,
        str(offer["consultation_type_id"]),
        appointment_datetime,
        fee_amount=consultation["fee"],
    )


def _sweep_expired_waitlist_offers() -> int:
    now = int(time.time())
    expired = _query_all_items(
        _waitlist_offer_table(),
        IndexName="gsi_live",
        KeyConditionExpression=Key("live_shard").eq("ACTIVE") & Key("expires_at").lte(now),
    )
    processed = 0
    for offer in expired:
        try:
            _waitlist_offer_table().update_item(
                Key={"slot_ref": offer["slot_ref"], "patient_id": offer["patient_id"]},
                UpdateExpression="SET #status = :expired REMOVE live_shard",
                ConditionExpression="#status = :open AND expires_at <= :now",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":expired": "EXPIRED",
                    ":open": "OPEN",
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
        try:
            _notify_waitlist_offer(
                str(offer["patient_id"]),
                str(offer["offer_id"]),
                slot,
                int(offer["expires_at"]),
                "WAITLIST_HOLD_OFFER_EXPIRED",
            )
        except Exception:
            logger.exception("Expired offer %s notification failed.", offer.get("offer_id"))
        slot_state = slot_availability_state(slot)
        if slot_state not in {"available", "full"}:
            release_slot_seat(slot, held=True)
            processed += 1
            continue
        _promote_waitlist(slot, seat_already_held=True)
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
    convert_booked_seat_to_hold(slot)

    try:
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
        try:
            convert_held_seat_to_booked(slot)
        except Exception:
            logger.exception("Cancellation compensation failed for appointment %s.", appointment_id)
        return 500, {"message": "Cancellation could not be completed. Please retry or contact support."}

    try:
        offer = _promote_waitlist(slot, seat_already_held=True)
    except Exception:
        logger.exception("Appointment %s was cancelled but waitlist promotion failed.", appointment_id)
        offer = None
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
        "waitlist_offer_issued": bool(offer),
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


def _remove_waitlist(patient_id: str, slot_id: str) -> tuple[int, dict[str, Any]]:
    response = _waitlist_table().delete_item(
        Key={"slot_ref": slot_id, "patient_id": patient_id},
        ConditionExpression="attribute_exists(slot_ref)",
        ReturnValues="ALL_OLD",
    )
    if not response.get("Attributes"):
        return 404, {"message": "Waitlist entry not found."}
    return 200, {"message": "You left the waitlist.", "slot_id": slot_id}



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

    if method == "GET" and path == "/patients/me/waitlists":
        return _response(200, _patient_waitlists(patient_id))

    if method == "POST" and path == "/patients/me/waitlists":
        payload = _get_request_body(event)
        slot_id = str(payload.get("slot_id") or "").strip()
        if not slot_id:
            return _response(400, {"message": "slot_id is required."})
        status_code, response = _join_waitlist(patient_id, slot_id)
        return _response(status_code, response)

    if method == "DELETE" and path == "/patients/me/waitlists":
        payload = _get_request_body(event)
        slot_id = str(payload.get("slot_id") or "").strip()
        if not slot_id:
            return _response(400, {"message": "slot_id is required."})
        status_code, response = _remove_waitlist(patient_id, slot_id)
        return _response(status_code, response)

    if (
        method == "POST"
        and len(path_parts) == 5
        and path_parts[:3] == ["patients", "me", "waitlist-offers"]
        and path_parts[4] == "accept"
    ):
        status_code, response = _accept_waitlist_offer(patient_id, path_parts[3])
        return _response(status_code, response)

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

        existing = execute_query(
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
            (patient_id, consultation_type_id, appointment_dt_utc.strftime("%Y-%m-%d %H:%M:%S")),
        )
        if existing:
            return _response(409, {"message": "You already have an appointment for this slot."})

        consultation_row = execute_query(
            """
            SELECT ct.consultation_type_id, ct.doctor_id, ct.fee, ct.capacity, ct.is_active,
                   catalog.duration_minutes,
                   d.first_name, d.last_name, d.is_verified
            FROM consultation_types ct
            JOIN doctors d ON d.doctor_id = ct.doctor_id
            JOIN consultation_catalog catalog ON catalog.consultation_type_id = ct.catalog_type_id
            WHERE ct.consultation_type_id = %s
            LIMIT 1
            """,
            (consultation_type_id,),
        )
        if not consultation_row:
            return _response(404, {"message": "Consultation type not found."})

        consultation = consultation_row[0]
        if not consultation.get("is_active"):
            return _response(409, {"message": "This consultation type is inactive."})
        if not consultation.get("is_verified"):
            return _response(409, {"message": "Doctor profile is not verified."})

        slot_id = slot_id_for_datetime(consultation_type_id, appointment_dt_utc)
        slot = get_slot_by_id(slot_id)
        slot_state = slot_availability_state(slot)
        if slot_state != "available":
            message = {
                "missing": "Selected consultation slot is not available yet.",
                "inactive": "This slot is inactive and cannot be booked.",
                "past": "Appointments must be scheduled in the future.",
                "full": "This slot is already full. Please choose another time.",
                "no_capacity": "This consultation type has no available seats.",
            }[slot_state]
            return _response(409, {"message": message})

        appointment_id = str(uuid.uuid4())
        try:
            reserve_slot_seat(
                slot,
                int(consultation.get("capacity") or 1),
            )
        except Exception:
            return _response(409, {"message": "This slot has just been filled. Please select another time."})

        try:
            with database_transaction() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO appointments (
                            appointment_id, patient_id, consultation_type_id, appointment_datetime, appointment_status
                        ) VALUES (%s, %s, %s, %s, 'CONFIRMED')
                        """,
                        (appointment_id, patient_id, consultation_type_id, appointment_dt_utc.strftime("%Y-%m-%d %H:%M:%S")),
                    )
                    cursor.execute(
                        """
                        INSERT INTO payments (
                            payment_id, appointment_id, amount, payment_status, payment_method
                        ) VALUES (%s, %s, %s, 'INITIATED', 'PAY_AT_CLINIC')
                        """,
                        (str(uuid.uuid4()), appointment_id, consultation["fee"]),
                    )
        except Exception:
            logger.exception("Unable to create appointment %s.", appointment_id)
            try:
                release_slot_seat(slot)
            except Exception:
                logger.exception("Could not release a reserved slot after appointment creation failed.")
            return _response(409, {"message": "Unable to confirm this appointment. Please refresh availability and try again."})

        try:
            publish_appointment_notification(
                appointment_id=appointment_id,
                event_type="APPOINTMENT_BOOKED",
                patient_id=patient_id,
                consultation_type_id=consultation_type_id,
                appointment_datetime=appointment_dt_utc,
                message="Your appointment is confirmed. Payment is due at the clinic.",
            )
        except Exception:
            logger.exception("Appointment %s was booked but its notification could not be recorded.", appointment_id)

        return _response(
            201,
            _patient_booking_response(
                appointment_id,
                consultation_type_id,
                appointment_dt_utc,
                fee_amount=consultation["fee"],
            ),
        )

    return _response(404, {"message": "Patient route not found."})
