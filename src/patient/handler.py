"""Patient dashboard and booking API."""

import base64
import json
import logging
import os
import uuid
from datetime import date, datetime, time as time_type, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import boto3
from boto3.dynamodb.conditions import Key

from shared.database import database_transaction, execute_query


logger = logging.getLogger(__name__)


AWS_REGION = os.environ.get("AWS_REGION", "ap-south-1")
ENVIRONMENT = os.environ.get("ENVIRONMENT", "dev")
SLOTS_TABLE_NAME = (
    os.environ.get("SLOTS_TABLE_NAME")
    or os.environ.get("SLOTS_TABLE")
    or f"MediSlot-Slots-{ENVIRONMENT}"
)

_dynamodb_resource = None
_slots_table = None


def get_slots_table():
    global _dynamodb_resource, _slots_table
    if _slots_table is None:
        _dynamodb_resource = boto3.resource("dynamodb", region_name=AWS_REGION)
        _slots_table = _dynamodb_resource.Table(SLOTS_TABLE_NAME)
    return _slots_table


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


def _slot_id_for_datetime(consultation_type_id: str, appointment_dt_utc: datetime) -> str:
    return f"{consultation_type_id}#{int(appointment_dt_utc.timestamp())}"


def _list_doctors(query_params: dict[str, str]) -> dict[str, Any]:
    search_term = (query_params.get("search") or query_params.get("q") or "").strip()
    specialization = (query_params.get("specialization") or "").strip()
    hospital = (query_params.get("hospital") or "").strip()
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
        sql += " AND LOWER(s.name) LIKE LOWER(%s) "
        params.append(f"%{specialization}%")

    if hospital:
        sql += " AND LOWER(h.name) LIKE LOWER(%s) "
        params.append(f"%{hospital}%")

    sql += " GROUP BY d.doctor_id, d.first_name, d.last_name, d.experience_years, d.timezone, h.name, h.address "
    sql += " ORDER BY d.experience_years DESC, d.last_name ASC "
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


def _get_doctor_profile(doctor_id: str) -> dict[str, Any] | None:
    rows = execute_query(
        """
        SELECT d.doctor_id,
               CONCAT_WS(' ', d.first_name, d.last_name) AS doctor_name,
               d.experience_years,
               d.qualification,
               d.languages,
               d.timezone,
               d.profile_image_key,
               h.name AS hospital_name,
               h.address AS hospital_address,
               h.phone_number AS hospital_phone,
               GROUP_CONCAT(DISTINCT s.name ORDER BY s.name SEPARATOR ', ') AS specializations
        FROM doctors d
        JOIN hospitals h ON h.hospital_id = d.hospital_id
        LEFT JOIN doctor_specializations ds ON ds.doctor_id = d.doctor_id
        LEFT JOIN specializations s ON s.specialization_id = ds.specialization_id
        WHERE d.doctor_id = %s AND d.is_verified = TRUE
        GROUP BY d.doctor_id, d.first_name, d.last_name, d.experience_years,
                 d.qualification, d.languages, d.timezone, d.profile_image_key,
                 h.name, h.address, h.phone_number
        LIMIT 1
        """,
        (doctor_id,),
    )
    if not rows:
        return None

    doctor = dict(rows[0])
    offerings = execute_query(
        """
        SELECT ct.consultation_type_id, ct.catalog_type_id, catalog.name,
               catalog.mode, catalog.duration_minutes, ct.fee, ct.capacity
        FROM consultation_types ct
        JOIN consultation_catalog catalog ON catalog.consultation_type_id = ct.catalog_type_id
        WHERE ct.doctor_id = %s AND ct.is_active = TRUE
        ORDER BY catalog.duration_minutes, catalog.name
        """,
        (doctor_id,),
    )
    doctor["consultations"] = offerings
    return doctor


def _get_doctor_slots(doctor_id: str, query_params: dict[str, str]) -> tuple[int, dict[str, Any]]:
    raw_day = (query_params.get("date") or "").strip()
    consultation_type_id = (query_params.get("consultation_type_id") or "").strip()
    try:
        selected_day = date.fromisoformat(raw_day)
    except ValueError:
        return 400, {"message": "A valid date in YYYY-MM-DD format is required."}

    doctor_rows = execute_query(
        "SELECT timezone FROM doctors WHERE doctor_id = %s AND is_verified = TRUE",
        (doctor_id,),
    )
    if not doctor_rows:
        return 404, {"message": "Doctor profile not found."}

    timezone_name = doctor_rows[0].get("timezone") or "UTC"
    try:
        local_timezone = ZoneInfo(timezone_name)
    except Exception:
        local_timezone = timezone.utc
        timezone_name = "UTC"

    local_start = datetime.combine(selected_day, time_type.min, tzinfo=local_timezone)
    local_end = datetime.combine(selected_day + timedelta(days=1), time_type.min, tzinfo=local_timezone)
    start_utc = local_start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_utc = local_end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    table = get_slots_table()
    slots: list[dict[str, Any]] = []
    query_args: dict[str, Any] = {
        "IndexName": "gsi_doctor_time",
        "KeyConditionExpression": Key("doctor_id").eq(doctor_id)
        & Key("start_time").between(start_utc, end_utc),
    }
    while True:
        result = table.query(**query_args)
        for item in result.get("Items", []):
            if consultation_type_id and item.get("consultation_type_id") != consultation_type_id:
                continue
            capacity = int(item.get("capacity") or 0)
            booked = int(item.get("booked_count") or 0)
            if item.get("is_active") is not False and not item.get("is_blocked") and booked < capacity:
                slots.append(item)
        last_key = result.get("LastEvaluatedKey")
        if not last_key:
            break
        query_args["ExclusiveStartKey"] = last_key

    slots.sort(key=lambda slot: slot.get("start_time", ""))
    return 200, {"date": selected_day.isoformat(), "timezone": timezone_name, "slots": slots}


def _get_patient_id(authorizer: dict[str, Any]) -> str | None:
    return str(authorizer.get("userId") or authorizer.get("sub") or "").strip() or None


def _get_slot_availability(consultation_type_id: str, appointment_datetime_utc: datetime) -> tuple[bool, str, dict[str, Any] | None]:
    slot_id = _slot_id_for_datetime(consultation_type_id, appointment_datetime_utc)
    slot = get_slots_table().get_item(Key={"slot_id": slot_id}).get("Item")
    if not slot:
        return False, "Selected consultation slot is not available yet.", None

    if slot.get("is_active") is False:
        return False, "This slot is inactive and cannot be booked.", slot

    capacity = int(slot.get("capacity") or 0)
    booked_count = int(slot.get("booked_count") or 0)
    if capacity <= 0:
        return False, "This consultation type has no available seats.", slot

    if booked_count >= capacity:
        return False, "This slot is already full. Please choose another time.", slot

    return True, "ok", slot


def _patient_booking_response(appointment_id: str, consultation_type_id: str, appointment_datetime_utc: datetime) -> dict[str, Any]:
    return {
        "appointment_id": appointment_id,
        "consultation_type_id": consultation_type_id,
        "appointment_datetime": appointment_datetime_utc.isoformat().replace("+00:00", "Z"),
        "status": "CONFIRMED",
    }


def lambda_handler(event: dict[str, Any] | None, context: Any = None) -> dict[str, Any]:
    event = event if isinstance(event, dict) else {}
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
                       AND pay.payment_status = 'SUCCESS'
                     ORDER BY pay.created_at DESC LIMIT 1),
                    ct.fee
                ) AS fee_amount,
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

    if method == "GET" and len(path_parts) == 3 and path_parts[:2] == ["patients", "doctors"]:
        doctor = _get_doctor_profile(path_parts[2])
        if not doctor:
            return _response(404, {"message": "Doctor profile not found."})
        return _response(200, {"doctor": doctor})

    if method == "GET" and len(path_parts) == 4 and path_parts[:2] == ["patients", "doctors"] and path_parts[3] == "slots":
        status, payload = _get_doctor_slots(
            path_parts[2], event.get("queryStringParameters") or {}
        )
        return _response(status, payload)

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
              AND appointment_status <> 'CANCELLED'
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

        valid_slot, message, slot = _get_slot_availability(consultation_type_id, appointment_dt_utc)
        if not valid_slot:
            return _response(409, {"message": message})

        appointment_id = str(uuid.uuid4())
        try:
            get_slots_table().update_item(
                Key={"slot_id": _slot_id_for_datetime(consultation_type_id, appointment_dt_utc)},
                UpdateExpression="SET booked_count = if_not_exists(booked_count, :zero) + :inc",
                ConditionExpression=(
                    "is_active = :active AND "
                    "(attribute_not_exists(booked_count) OR booked_count < :capacity)"
                ),
                ExpressionAttributeValues={
                    ":inc": 1,
                    ":zero": 0,
                    ":capacity": int(consultation.get("capacity") or 1),
                    ":active": True,
                },
                ReturnValues="UPDATED_NEW",
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
        except Exception:
            try:
                get_slots_table().update_item(
                    Key={"slot_id": _slot_id_for_datetime(consultation_type_id, appointment_dt_utc)},
                    UpdateExpression="SET booked_count = booked_count - :one",
                    ConditionExpression="booked_count > :zero",
                    ExpressionAttributeValues={":one": 1, ":zero": 0},
                )
            except Exception:
                logger.exception("Could not release a reserved slot after appointment creation failed.")
            return _response(409, {"message": "Unable to confirm this appointment. Please refresh availability and try again."})

        return _response(201, _patient_booking_response(appointment_id, consultation_type_id, appointment_dt_utc))

    return _response(404, {"message": "Patient route not found."})
