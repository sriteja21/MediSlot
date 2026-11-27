import json
import logging
import uuid
import boto3
from datetime import datetime, timezone
from typing import Any

from shared.booking import calculate_refund, load_refund_policy, slot_sk_for
from shared.database import database_transaction, execute_query
from shared.notifications import publish_appointment_notification
from read_api.service import get_slot_by_id, release_slot_seat, slot_id_for_datetime

logger = logging.getLogger(__name__)

OWNER_COLUMN = {"patient": "patient_id", "doctor": "doctor_id"}
CANCEL_STATUS = {
    "patient": "CANCELLED_BY_PATIENT",
    "doctor": "CANCELLED_BY_DOCTOR",
}


def _response(status_code: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload, default=str),
    }


def _authorizer(event: dict[str, Any]) -> dict[str, Any]:
    return ((event.get("requestContext") or {}).get("authorizer") or {})


def _actor(event: dict[str, Any], role: str) -> str:
    authorizer = _authorizer(event)
    return str(authorizer.get("userId") or authorizer.get("sub") or "").strip()


def _load_appointment(appointment_id: str, role: str, actor_id: str) -> dict[str, Any] | None:
    rows = execute_query(
        f"""SELECT a.appointment_id, a.patient_id, a.doctor_id,
                    a.consultation_type_id, a.appointment_datetime,
                    a.appointment_status, p.payment_id,
                    COALESCE(p.amount, ct.fee) AS fee_amount
             FROM appointments a
             JOIN consultation_types ct ON ct.consultation_type_id = a.consultation_type_id
             LEFT JOIN payments p ON p.appointment_id = a.appointment_id
             WHERE a.appointment_id = %s AND a.{OWNER_COLUMN[role]} = %s
             LIMIT 1""",
        (appointment_id, actor_id),
    )
    return rows[0] if rows else None


def cancel_appointment(appointment_id: str, role: str, actor_id: str) -> tuple[int, dict[str, Any]]:
    appt = _load_appointment(appointment_id, role, actor_id)
    if not appt:
        return 404, {"message": "Appointment not found."}
    if appt["appointment_status"] != "CONFIRMED":
        return 409, {"message": "Only confirmed appointments can be cancelled."}

    start = appt["appointment_datetime"]
    if isinstance(start, datetime):
        start = start.replace(tzinfo=timezone.utc) if start.tzinfo is None else start.astimezone(timezone.utc)
    refund = calculate_refund(
        appt["fee_amount"], start, datetime.now(timezone.utc),
        cancellation_by=role, policy=load_refund_policy(),
    )

    with database_transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """UPDATE appointments
                   SET appointment_status = %s, cancelled_at = UTC_TIMESTAMP(3)
                   WHERE appointment_id = %s AND appointment_status = 'CONFIRMED'""",
                (CANCEL_STATUS[role], appointment_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Appointment changed concurrently.")
            if appt.get("payment_id") and refund["refund_amount"] > 0:
                cursor.execute(
                    """INSERT INTO refunds
                       (refund_id, payment_id, refund_amount, refund_status)
                       VALUES (%s, %s, %s, 'PENDING')""",
                    (str(uuid.uuid4()), appt["payment_id"], refund["refund_amount"]),
                )

    slot_id = slot_id_for_datetime(str(appt["consultation_type_id"]), start)
    slot = get_slot_by_id(slot_id)
    if slot:
        release_slot_seat(slot)

    message = f"Appointment {appointment_id} was cancelled. Refund ledger amount: INR {refund['refund_amount']:.2f}."
    try:
        publish_appointment_notification(
            appointment_id=appointment_id,
            event_type=f"APPOINTMENT_CANCELLED_BY_{role.upper()}",
            patient_id=str(appt["patient_id"]),
            consultation_type_id=str(appt["consultation_type_id"]),
            appointment_datetime=start,
            message=message,
        )
    except Exception:
        logger.exception("Appointment %s cancellation notification failed.", appointment_id)

    return 200, {
        "appointment_id": appointment_id,
        "status": CANCEL_STATUS[role],
        "refund_amount": refund["refund_amount"],
        "refund_percent": refund["refund_percent"],
        "refund_tier": refund["tier"],
    }


def complete_appointment(appointment_id: str, doctor_id: str) -> tuple[int, dict[str, Any]]:
    appt = _load_appointment(appointment_id, "doctor", doctor_id)
    if not appt:
        return 404, {"message": "Appointment not found."}
    if appt["appointment_status"] != "CONFIRMED":
        return 409, {"message": "Only confirmed appointments can be completed."}
    start = appt["appointment_datetime"]
    if isinstance(start, datetime) and start.replace(tzinfo=start.tzinfo or timezone.utc) > datetime.now(timezone.utc):
        return 409, {"message": "The appointment cannot be completed before its start time."}

    with database_transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE appointments SET appointment_status = 'COMPLETED' WHERE appointment_id = %s AND appointment_status = 'CONFIRMED'",
                (appointment_id,),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Appointment changed concurrently.")
            cursor.execute(
                """UPDATE payments SET payment_status = 'SUCCESS'
                   WHERE appointment_id = %s AND payment_status = 'INITIATED'""",
                (appointment_id,),
            )
    return 200, {"appointment_id": appointment_id, "status": "COMPLETED"}


def mark_no_show(appointment_id: str, doctor_id: str) -> tuple[int, dict[str, Any]]:
    appt = _load_appointment(appointment_id, "doctor", doctor_id)
    if not appt:
        return 404, {"message": "Appointment not found."}
    if appt["appointment_status"] != "CONFIRMED":
        return 409, {"message": "Only confirmed appointments can be marked as no-show."}

    with database_transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE appointments SET appointment_status = 'NO_SHOW' WHERE appointment_id = %s AND appointment_status = 'CONFIRMED'",
                (appointment_id,),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Appointment changed concurrently.")

    return 200, {"appointment_id": appointment_id, "status": "NO_SHOW"}


def _start_reschedule(event: dict[str, Any], appointment_id: str, patient_id: str) -> tuple[int, dict[str, Any]]:
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return 400, {"message": "Request body must be valid JSON."}
    consultation_type_id = str(body.get("consultation_type_id") or "").strip()
    appointment_datetime = str(body.get("appointment_datetime") or "").strip()
    if not consultation_type_id or not appointment_datetime:
        return 400, {"message": "consultation_type_id and appointment_datetime are required."}

    rows = execute_query(
        "SELECT reschedule_count FROM appointments WHERE appointment_id = %s AND patient_id = %s LIMIT 1",
        (appointment_id, patient_id),
    )
    if not rows:
        return 404, {"message": "Appointment not found."}

    sfn = boto3.client("stepfunctions", region_name=os.environ.get("AWS_REGION", "ap-south-1"))
    count = int(rows[0].get("reschedule_count") or 0)
    try:
        execution = sfn.start_execution(
            stateMachineArn=os.environ["RESCHEDULE_STATE_MACHINE_ARN"],
            name=f"{appointment_id}-{count + 1}",
            input=json.dumps({
                "appointment_id": appointment_id,
                "patient_id": patient_id,
                "new_consultation_type_id": consultation_type_id,
                "new_datetime": appointment_datetime,
                "fail_at": body.get("fail_at") if body.get("fail_at") else None,
            }),
        )
    except sfn.exceptions.ExecutionAlreadyExists:
        return 409, {"message": "A reschedule is already in progress."}
    return 202, {"reschedule_id": execution["executionArn"], "status": "PENDING"}


def lambda_handler(event: dict[str, Any] | None, context: Any = None) -> dict[str, Any]:
    event = event if isinstance(event, dict) else {}
    request_context = event.get("requestContext") or {}
    method = str(event.get("httpMethod") or (request_context.get("http") or {}).get("method") or "").upper()
    path = str(event.get("path") or event.get("rawPath") or (request_context.get("http") or {}).get("path") or "").rstrip("/").lower()
    parts = [part for part in path.split("/") if part]
    if parts and parts[0] in {"dev", "test", "prod", "v1", "v2"}:
        parts = parts[1:]
    normalized = "/" + "/".join(parts)
    path_parameters = event.get("pathParameters") or {}
    appointment_id = str(path_parameters.get("appointment_id") or "").strip()
    authorizer = _authorizer(event)

    if not appointment_id:
        return _response(400, {"message": "appointment_id is required."})

    if method == "POST" and normalized.endswith("/cancel"):
        role = "doctor" if normalized.startswith("/doctors/") else "patient"
        actor_id = _actor(event, role)
        if not actor_id:
            return _response(401, {"message": "Authentication required."})
        status_code, payload = cancel_appointment(appointment_id, role, actor_id)
        return _response(status_code, payload)

    if method == "POST" and normalized.endswith("/reschedule"):
        patient_id = str(authorizer.get("userId") or authorizer.get("sub") or "").strip()
        if str(authorizer.get("role", "")).lower() != "patient" or not patient_id:
            return _response(403, {"message": "Patient role required."})
        status_code, payload = _start_reschedule(event, appointment_id, patient_id)
        return _response(status_code, payload)

    if method == "POST" and normalized.endswith("/complete"):
        doctor_id = str(authorizer.get("userId") or authorizer.get("sub") or "").strip()
        if str(authorizer.get("role", "")).lower() != "doctor" or not doctor_id:
            return _response(403, {"message": "Doctor role required."})
        status_code, payload = complete_appointment(appointment_id, doctor_id)
        return _response(status_code, payload)

    if method == "POST" and normalized.endswith("/no-show"):
        doctor_id = str(authorizer.get("userId") or authorizer.get("sub") or "").strip()
        if str(authorizer.get("role", "")).lower() != "doctor" or not doctor_id:
            return _response(403, {"message": "Doctor role required."})
        status_code, payload = mark_no_show(appointment_id, doctor_id)
        return _response(status_code, payload)

    return _response(404, {"message": "Route not found."})
