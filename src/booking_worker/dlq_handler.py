import logging
from datetime import datetime

from handler import (
    TERMINAL_STATUSES,
    _appointment_ids,
    _get_request,
    _mark_confirmed,
    _mark_declined_without_reservation,
    _release_reserved_slot_and_decline,
    _request_from_message,
)
from shared.booking import slot_sk_for
from shared.database import execute_query

logger = logging.getLogger()
FAILURE_MESSAGE = "We could not complete your booking. Please try again."
CANCELLED = {"CANCELLED", "CANCELLED_BY_PATIENT", "CANCELLED_BY_DOCTOR"}


def _appointment_is_active(appointment_id):
    rows = execute_query(
        "SELECT appointment_status FROM appointments WHERE appointment_id = %s LIMIT 1",
        (appointment_id,),
    )
    return bool(rows) and rows[0]["appointment_status"] not in CANCELLED


def lambda_handler(event, context):
    failures = []
    for record in event.get("Records", []):
        try:
            user_id, scope_key = _request_from_message(record)
            request = _get_request(user_id, scope_key)
            if not request or request.get("status") in TERMINAL_STATUSES:
                continue

            request["user_id"], request["scope_key"] = user_id, scope_key
            requested_start = datetime.fromisoformat(str(request["appointment_datetime"]).replace("Z", "+00:00"))
            request["slot_sk"] = slot_sk_for(request["consultation_type_id"], requested_start)
            appointment_id, _ = _appointment_ids(request)
            if _appointment_is_active(appointment_id):
                _mark_confirmed(request, appointment_id, True)
            elif request.get("slot_reserved"):
                _release_reserved_slot_and_decline(request, FAILURE_MESSAGE)
            else:
                _mark_declined_without_reservation(request, FAILURE_MESSAGE)
        except Exception:
            logger.exception("DLQ processor failed for a record.")
            failures.append({"itemIdentifier": record.get("messageId", "")})
    return {"batchItemFailures": failures}
