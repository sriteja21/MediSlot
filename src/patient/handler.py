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
            ct.name AS consultation_type,
            CONCAT_WS(' ', d.first_name, d.last_name) AS doctor_name,
            h.name AS hospital_name,
            h.address AS hospital_city
        FROM appointments a
        JOIN consultation_types ct ON ct.consultation_type_id = a.consultation_type_id
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
