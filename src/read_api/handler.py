"""Shared doctor and patient read API."""

import json
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfoNotFoundError

from shared.database import execute_query

if __package__:
    from .service import (
        local_day_utc_bounds,
        query_doctor_slots,
        slot_availability_state,
        slot_response,
    )
else:
    from service import (
        local_day_utc_bounds,
        query_doctor_slots,
        slot_availability_state,
        slot_response,
    )


logger = logging.getLogger(__name__)


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _response(status_code: int, payload: Any) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "GET,OPTIONS",
        },
        "body": json.dumps(payload, default=_json_value),
    }


def _doctor_schedule(
    doctor_id: str,
    selected_date: date,
    timezone_name: str,
) -> dict[str, Any]:
    utc_start, utc_end = local_day_utc_bounds(selected_date, timezone_name)
    slots = query_doctor_slots(doctor_id, utc_start, utc_end)
    utc_start_db = utc_start.replace(tzinfo=None)
    utc_end_db = utc_end.replace(tzinfo=None)

    count_rows = execute_query(
        """
        SELECT
            a.consultation_type_id,
            a.appointment_datetime,
            COUNT(*) AS total_count,
            SUM(CASE WHEN a.appointment_status = 'CONFIRMED' THEN 1 ELSE 0 END) AS confirmed_count,
            SUM(CASE WHEN a.appointment_status = 'COMPLETED' THEN 1 ELSE 0 END) AS completed_count,
            SUM(CASE WHEN a.appointment_status IN ('CONFIRMED', 'COMPLETED') THEN 1 ELSE 0 END) AS active_count
        FROM appointments a
        JOIN consultation_types ct ON ct.consultation_type_id = a.consultation_type_id
        WHERE ct.doctor_id = %s
          AND a.appointment_datetime >= %s
          AND a.appointment_datetime < %s
        GROUP BY a.consultation_type_id, a.appointment_datetime
        """,
        (doctor_id, utc_start_db, utc_end_db),
    )
    counts_by_slot = {}
    for row in count_rows:
        appointment_datetime = row["appointment_datetime"]
        if appointment_datetime.tzinfo is None:
            appointment_datetime = appointment_datetime.replace(tzinfo=timezone.utc)
        slot_id = (
            f"{row['consultation_type_id']}#{int(appointment_datetime.timestamp())}"
        )
        counts_by_slot[slot_id] = {
            "total": int(row["total_count"] or 0),
            "confirmed": int(row["confirmed_count"] or 0),
            "completed": int(row["completed_count"] or 0),
            "active": int(row["active_count"] or 0),
        }

    for slot in slots:
        counts = counts_by_slot.get(
            slot.get("slot_id"),
            {"total": 0, "confirmed": 0, "completed": 0, "active": 0},
        )
        slot["appointment_count"] = counts["active"]
        slot["confirmed_count"] = counts["confirmed"]
        slot["total_appointments"] = counts["total"]
        if "booked_count" not in slot:
            slot["booked_count"] = counts["active"]
    slots = [slot_response(slot) for slot in slots]

    appointments = execute_query(
        """
        SELECT
            a.appointment_id AS id,
            a.patient_id,
            CONCAT_WS(' ', p.first_name, p.last_name) AS patient_name,
            ct.doctor_id,
            a.consultation_type_id,
            a.appointment_datetime AS slot_start_utc,
            a.appointment_status AS status,
            COALESCE(
                (SELECT pay.amount FROM payments pay
                 WHERE pay.appointment_id = a.appointment_id
                 ORDER BY pay.created_at DESC LIMIT 1),
                ct.fee
            ) AS fee_amount,
            catalog.duration_minutes,
            a.created_at AS booked_at
        FROM appointments a
        JOIN consultation_types ct ON ct.consultation_type_id = a.consultation_type_id
        JOIN consultation_catalog catalog ON catalog.consultation_type_id = ct.catalog_type_id
        LEFT JOIN patients p ON p.patient_id = a.patient_id
        WHERE ct.doctor_id = %s
          AND a.appointment_datetime >= %s
          AND a.appointment_datetime < %s
        ORDER BY a.appointment_datetime ASC
        """,
        (doctor_id, utc_start_db, utc_end_db),
    )
    for appointment in appointments:
        appointment["fee_amount"] = float(appointment["fee_amount"])
        appointment_datetime = appointment["slot_start_utc"]
        if appointment_datetime.tzinfo is None:
            appointment_datetime = appointment_datetime.replace(tzinfo=timezone.utc)
        appointment["slot_start_utc"] = appointment_datetime.isoformat()
        appointment["slot_id"] = (
            f"{appointment['consultation_type_id']}#{int(appointment_datetime.timestamp())}"
        )
        appointment["slot_end_utc"] = (
            appointment_datetime
            + timedelta(minutes=int(appointment.pop("duration_minutes")))
        ).isoformat()

    slots.sort(key=lambda slot: str(slot.get("start_time", "")))
    return {
        "date": selected_date.isoformat(),
        "slots": slots,
        "appointments": appointments,
        "total_slots": len(slots),
        "total_appointments": len(appointments),
    }


def _patient_slots(
    doctor_id: str,
    start_date: date,
    end_date: date,
    consultation_type_id: str,
    include_full: bool,
) -> tuple[int, dict[str, Any]]:
    doctor_rows = execute_query(
        "SELECT timezone FROM doctors WHERE doctor_id = %s AND is_verified = TRUE",
        (doctor_id,),
    )
    if not doctor_rows:
        return 404, {"message": "Doctor profile not found."}

    timezone_name = str(doctor_rows[0].get("timezone") or "UTC")
    try:
        utc_start, _ = local_day_utc_bounds(start_date, timezone_name)
        _, utc_end = local_day_utc_bounds(end_date, timezone_name)
    except Exception:
        logger.exception("Invalid timezone configured for doctor %s.", doctor_id)
        timezone_name = "UTC"
        utc_start, _ = local_day_utc_bounds(start_date, timezone_name)
        _, utc_end = local_day_utc_bounds(end_date, timezone_name)

    slots = []
    for slot in query_doctor_slots(
        doctor_id,
        utc_start,
        utc_end,
        include_end=True,
    ):
        if consultation_type_id and slot.get("consultation_type_id") != consultation_type_id:
            continue
        state = slot_availability_state(slot)
        if state == "available" or (include_full and state == "full"):
            response = slot_response(slot)
            response["available"] = state == "available"
            slots.append(response)

    slots.sort(key=lambda slot: slot.get("start_time", ""))
    return 200, {
        "date": start_date.isoformat() if start_date == end_date else None,
        "from": start_date.isoformat(),
        "to": end_date.isoformat(),
        "timezone": timezone_name,
        "slots": slots,
    }


def _consultation_types(
    doctor_id: str,
    include_all: bool,
    *,
    order_by_duration: bool = False,
) -> list[dict[str, Any]]:
    query = """
        SELECT ct.consultation_type_id, ct.doctor_id,
               catalog.consultation_type_id AS catalog_type_id,
               catalog.name, catalog.mode, catalog.duration_minutes,
               ct.fee, ct.capacity, ct.is_active
        FROM consultation_types ct
        JOIN consultation_catalog catalog
          ON catalog.consultation_type_id = ct.catalog_type_id
        WHERE ct.doctor_id = %s
    """
    if not include_all:
        query += " AND ct.is_active = TRUE"
    if order_by_duration:
        query += " ORDER BY catalog.duration_minutes ASC, catalog.name ASC"
    else:
        query += " ORDER BY catalog.name ASC"
    rows = execute_query(query, (doctor_id,))
    for row in rows:
        row["fee"] = float(row["fee"])
        row["capacity"] = int(row["capacity"])
        row["duration_minutes"] = int(row["duration_minutes"])
        row["is_active"] = bool(row["is_active"])
    return rows


def _doctor_consultation_types(doctor_id: str, include_all: bool) -> list[dict[str, Any]]:
    rows = _consultation_types(doctor_id, include_all)
    return [
        {
            "id": row["consultation_type_id"],
            "doctor_id": row["doctor_id"],
            "catalog_type_id": row["catalog_type_id"],
            "kind": row["name"],
            "duration_minutes": row["duration_minutes"],
            "fee_amount": row["fee"],
            "capacity": row["capacity"],
            "is_active": row["is_active"],
        }
        for row in rows
    ]


def _patient_doctor_profile(doctor_id: str) -> dict[str, Any] | None:
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
    doctor["consultations"] = [
        {
            "consultation_type_id": row["consultation_type_id"],
            "catalog_type_id": row["catalog_type_id"],
            "name": row["name"],
            "mode": row["mode"],
            "duration_minutes": row["duration_minutes"],
            "fee": row["fee"],
            "capacity": row["capacity"],
        }
        for row in _consultation_types(
            doctor_id,
            include_all=False,
            order_by_duration=True,
        )
    ]
    return doctor


def lambda_handler(event: dict[str, Any] | None, context: Any = None) -> dict[str, Any]:
    event = event if isinstance(event, dict) else {}
    request_context = event.get("requestContext") or {}
    authorizer = request_context.get("authorizer") or {}
    role = str(authorizer.get("role") or "").lower()
    user_id = str(authorizer.get("userId") or "").strip()
    if role not in {"doctor", "patient"} or not user_id:
        return _response(401, {"message": "Unauthorized."})

    request = request_context.get("http") or {}
    method = str(event.get("httpMethod") or request.get("method") or "").upper()
    if method != "GET":
        return _response(405, {"message": "Method not allowed."})

    raw_path = str(
        event.get("path")
        or event.get("rawPath")
        or request.get("path")
        or ""
    ).rstrip("/")
    path_parts = [part for part in raw_path.split("/") if part]
    if path_parts and path_parts[0] in {"dev", "test", "prod", "v1", "v2"}:
        path_parts = path_parts[1:]
    path = "/" + "/".join(path_parts)

    try:
        if path == "/doctors/me/consultation-types":
            if role != "doctor":
                return _response(403, {"error": "Forbidden: Doctor role required."})
            query_params = event.get("queryStringParameters") or {}
            include_all = str(query_params.get("all") or "").lower() == "true"
            return _response(
                200,
                _doctor_consultation_types(user_id, include_all),
            )

        if len(path_parts) == 3 and path_parts[:2] == ["patients", "doctors"]:
            if role != "patient":
                return _response(403, {"message": "Patient role required."})
            path_parameters = event.get("pathParameters") or {}
            doctor_id = str(
                path_parameters.get("doctor_id")
                or (path_parts[2] if len(path_parts) == 3 else "")
            ).strip()
            if not doctor_id:
                return _response(404, {"message": "Doctor profile not found."})
            doctor = _patient_doctor_profile(doctor_id)
            if not doctor:
                return _response(404, {"message": "Doctor profile not found."})
            return _response(200, {"doctor": doctor})

        if path != "/slots":
            return _response(404, {"message": "Route not found."})
    except Exception:
        logger.exception("Unable to load read API data for path=%s.", path)
        return _response(500, {"message": "Unable to load requested data."})

    query_params = event.get("queryStringParameters") or {}
    raw_date = str(query_params.get("date") or "").strip()
    raw_from = str(query_params.get("from") or "").strip()
    raw_to = str(query_params.get("to") or "").strip()
    if raw_from or raw_to:
        if role != "patient":
            return _response(400, {"error": "Doctor schedule reads require a single date."})
        if not raw_from or not raw_to:
            return _response(400, {"message": "Both from and to dates are required."})
        try:
            range_start = date.fromisoformat(raw_from)
            range_end = date.fromisoformat(raw_to)
        except ValueError:
            return _response(400, {"message": "from and to must use YYYY-MM-DD format."})
        if range_end < range_start:
            return _response(400, {"message": "to must be on or after from."})
        if (range_end - range_start).days >= 30:
            return _response(400, {"message": "Slot ranges cannot exceed 30 days."})
        if role == "patient":
            doctor_id = str(query_params.get("doctor_id") or "").strip()
            if not doctor_id:
                return _response(400, {"message": "doctor_id is required."})
            status_code, payload = _patient_slots(
                doctor_id,
                range_start,
                range_end,
                str(query_params.get("consultation_type_id") or "").strip(),
                str(query_params.get("include_full") or "").lower() == "true",
            )
            return _response(status_code, payload)

    if not raw_date:
        if role == "doctor":
            return _response(
                400,
                {"error": "Query parameter 'date' (YYYY-MM-DD) is required."},
            )
        return _response(
            400,
            {"message": "A valid date in YYYY-MM-DD format is required."},
        )
    try:
        selected_date = date.fromisoformat(raw_date)
    except ValueError:
        if role == "doctor":
            return _response(
                400,
                {"error": "Query parameter 'date' must be in YYYY-MM-DD format."},
            )
        return _response(
            400,
            {"message": "A valid date in YYYY-MM-DD format is required."},
        )

    try:
        if role == "doctor":
            doctor_rows = execute_query(
                "SELECT timezone FROM doctors WHERE doctor_id = %s",
                (user_id,),
            )
            if not doctor_rows:
                return _response(404, {"error": "Doctor not found."})
            timezone_name = str(doctor_rows[0].get("timezone") or "UTC")
            try:
                payload = _doctor_schedule(user_id, selected_date, timezone_name)
            except ZoneInfoNotFoundError:
                return _response(400, {"error": "Doctor timezone is invalid."})
            return _response(200, payload)

        doctor_id = str(query_params.get("doctor_id") or "").strip()
        if not doctor_id:
            return _response(400, {"message": "doctor_id is required."})
        status_code, payload = _patient_slots(
            doctor_id,
            selected_date,
            selected_date,
            str(query_params.get("consultation_type_id") or "").strip(),
            False,
        )
        return _response(status_code, payload)
    except Exception:
        logger.exception("Unable to load slot data for role=%s.", role)
        return _response(500, {"message": "Unable to load slots."})
