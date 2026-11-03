"""Read-only patient dashboard API."""

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from shared.database import execute_query


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

    if method != "GET" or not path.endswith("/patients/me/dashboard"):
        return _response(404, {"message": "Patient route not found."})

    authorizer = request_context.get("authorizer") or {}
    if str(authorizer.get("role", "")).lower() != "patient":
        return _response(403, {"message": "Patient role required."})

    patient_id = authorizer.get("userId")
    if not patient_id:
        return _response(401, {"message": "Authentication required."})

    patient_rows = execute_query(
        """
        SELECT id, full_name, email
        FROM patients
        WHERE id = %s AND deleted_at IS NULL
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
          AND status = 'CONFIRMED'
          AND slot_start_utc >= UTC_TIMESTAMP()
        """,
        (patient_id,),
    )
    appointments = execute_query(
        """
        SELECT
            a.id,
            a.slot_start_utc,
            a.slot_end_utc,
            a.status,
            a.fee_amount,
            ct.kind AS consultation_type,
            d.full_name AS doctor_name,
            h.name AS hospital_name,
            h.city AS hospital_city
        FROM appointments a
        JOIN doctors d ON d.id = a.doctor_id
        JOIN consultation_types ct ON ct.id = a.consultation_type_id
        JOIN hospitals h ON h.id = d.hospital_id
        WHERE a.patient_id = %s
        ORDER BY
            CASE
                WHEN a.status = 'CONFIRMED' AND a.slot_start_utc >= UTC_TIMESTAMP() THEN 0
                ELSE 1
            END,
            a.slot_start_utc ASC
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
