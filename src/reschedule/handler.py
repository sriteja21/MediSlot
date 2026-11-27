import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from botocore.exceptions import ClientError

from read_api.service import (
    convert_booked_seat_to_hold,
    get_slot_by_id,
    get_slots_table,
    release_slot_seat,
    reserve_slot_seat,
    slot_id_for_datetime,
)
from shared.database import database_transaction, execute_query
from shared.notifications import publish_domain_event
from shared.waitlist import waitlist_table

logger = logging.getLogger(__name__)
MAX_RESCHEDULES = 2


class RescheduleRejected(Exception):
    pass


class SlotFull(Exception):
    pass


class TransientSeatError(Exception):
    pass


def _utc(value: Any) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def validate(input_data: dict[str, Any]) -> dict[str, Any]:
    appointment_id = str(input_data["appointment_id"])
    patient_id = str(input_data["patient_id"])
    rows = execute_query(
        """SELECT a.appointment_id, a.patient_id, a.doctor_id, a.consultation_type_id,
                  a.appointment_datetime, a.appointment_status, a.reschedule_count
           FROM appointments a WHERE a.appointment_id = %s AND a.patient_id = %s LIMIT 1""",
        (appointment_id, patient_id),
    )
    if not rows:
        raise RescheduleRejected("Appointment not found")
    appt = rows[0]
    if appt["appointment_status"] != "CONFIRMED":
        raise RescheduleRejected("Only confirmed appointments can be rescheduled")
    if int(appt.get("reschedule_count") or 0) >= MAX_RESCHEDULES:
        raise RescheduleRejected("Reschedule limit reached")

    original = _utc(appt["appointment_datetime"])
    if (original - datetime.now(timezone.utc)).total_seconds() < 6 * 3600:
        raise RescheduleRejected("Reschedule cutoff has passed")

    target_id = slot_id_for_datetime(str(input_data["new_consultation_type_id"]), _utc(input_data["new_datetime"]))
    target = get_slot_by_id(target_id)
    if not target or str(target.get("doctor_id")) != str(appt["doctor_id"]):
        raise RescheduleRejected("Target slot is not available for the same doctor")
    if int(target.get("capacity") or 0) <= int(target.get("booked_count") or 0) + int(target.get("held_count") or 0):
        raise SlotFull("Target slot is full")

    return {
        "appointment_id": appointment_id,
        "patient_id": patient_id,
        "original_datetime": original.isoformat(),
        "original_consultation_type_id": str(appt["consultation_type_id"]),
        "target_slot_id": target_id,
        "doctor_id": str(appt["doctor_id"]),
        "new_datetime": _utc(input_data["new_datetime"]).isoformat(),
        "new_consultation_type_id": str(input_data["new_consultation_type_id"]),
        "reschedule_count": int(appt.get("reschedule_count") or 0),
    }


def claim_target(ctx: dict[str, Any]) -> dict[str, Any]:
    target = get_slot_by_id(str(ctx["target_slot_id"]))
    if not target:
        raise SlotFull("Target slot no longer exists")
    try:
        reserve_slot_seat(target, int(target["capacity"]))
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise SlotFull("Target slot is full") from exc
        raise
    return {"target_claimed": True}


def move_appointment(ctx: dict[str, Any]) -> dict[str, Any]:
    with database_transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """UPDATE appointments
                   SET appointment_datetime = %s, consultation_type_id = %s,
                       reschedule_count = reschedule_count + 1
                   WHERE appointment_id = %s AND appointment_status = 'CONFIRMED'
                     AND appointment_datetime = %s""",
                (
                    _utc(ctx["new_datetime"]).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                    ctx["new_consultation_type_id"],
                    ctx["appointment_id"],
                    _utc(ctx["original_datetime"]).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                ),
            )
            if cursor.rowcount != 1:
                raise RescheduleRejected("Appointment could not be moved")
    return {"appointment_moved": True}


def release_original(ctx: dict[str, Any]) -> dict[str, Any]:
    if os.environ.get("ENABLE_FAULT_INJECTION", "false").lower() == "true":
        if ctx.get("fail_at") == "release_original":
            raise TransientSeatError("Injected release_original failure")

    original_id = slot_id_for_datetime(str(ctx["original_consultation_type_id"]), _utc(ctx["original_datetime"]))
    slot = get_slot_by_id(original_id)
    if not slot:
        raise TransientSeatError("Original slot disappeared")

    try:
        waiters = waitlist_table().query(
            IndexName="lsi_queue",
            KeyConditionExpression="slot_ref = :slot",
            ExpressionAttributeValues={":slot": original_id},
            Limit=1,
        ).get("Items", [])
        if waiters:
            convert_booked_seat_to_hold(slot)
            publish_domain_event("SlotSeatFreed", {"slot_id": original_id, "seat_held": True}, source="medislot.waitlist")
        else:
            release_slot_seat(slot)
    except Exception as exc:
        raise TransientSeatError(str(exc)) from exc
    return {"original_released": True}


def revert_appointment(ctx: dict[str, Any]) -> dict[str, Any]:
    with database_transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """UPDATE appointments
                   SET appointment_datetime = %s, consultation_type_id = %s,
                       reschedule_count = reschedule_count - 1
                   WHERE appointment_id = %s AND appointment_status = 'CONFIRMED'
                     AND appointment_datetime = %s AND consultation_type_id = %s""",
                (
                    _utc(ctx["original_datetime"]).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                    ctx["original_consultation_type_id"],
                    ctx["appointment_id"],
                    _utc(ctx["new_datetime"]).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                    ctx["new_consultation_type_id"],
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Could not revert appointment")
    from shared.metrics import emit_metric
    emit_metric("ReschedulesCompensated")
    return {"reverted": True}


def release_target(ctx: dict[str, Any]) -> dict[str, Any]:
    target = get_slot_by_id(str(ctx["target_slot_id"]))
    if not target:
        return {"target_released": False}
    release_slot_seat(target)
    return {"target_released": True}


def publish_rescheduled(ctx: dict[str, Any]) -> dict[str, Any]:
    next_count = int(ctx["reschedule_count"]) + 1
    publish_domain_event("AppointmentRescheduled", {
        "event_key": f"{ctx['appointment_id']}#{next_count}",
        "appointment_id": ctx["appointment_id"],
        "consultation_type_id": ctx["new_consultation_type_id"],
        "appointment_datetime": ctx["new_datetime"],
        "message": "Your appointment was rescheduled.",
        "recipients": [],
    })
    return {"published": True}


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    action = str(event.get("action") or "")
    input_data = event.get("input") or event
    if action == "validate":
        return validate(input_data)
    ctx = input_data
    if action == "claim_target":
        return claim_target(ctx)
    if action == "move_appointment":
        return move_appointment(ctx)
    if action == "release_original":
        return release_original(ctx)
    if action == "revert_appointment":
        return revert_appointment(ctx)
    if action == "release_target":
        return release_target(ctx)
    if action == "publish_rescheduled":
        return publish_rescheduled(ctx)
    raise ValueError(f"Unknown reschedule action: {action}")
