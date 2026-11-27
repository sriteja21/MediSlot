"""Shared DynamoDB waitlist hold and promotion operations."""

import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from read_api.service import (
    convert_held_seat_to_booked,
    get_slot_by_id,
    get_slots_table,
    release_slot_seat,
    slot_availability_state,
    slot_table_key,
)
from shared.config import get_ssm_parameter
from shared.database import database_transaction, execute_query
from shared.notifications import publish_domain_event

logger = logging.getLogger(__name__)
_dynamodb = None


def _table(environment_name: str) -> Any:
    global _dynamodb
    table_name = os.environ.get(environment_name, "").strip()
    if not table_name:
        raise RuntimeError(f"{environment_name} is not configured.")
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "ap-south-1"))
    return _dynamodb.Table(table_name)


def waitlist_table() -> Any:
    return _table("WAITLIST_TABLE")


def waitlist_offer_table() -> Any:
    return _table("WAITLIST_OFFER_TABLE")


def waitlist_hold_seconds() -> int:
    parameter_name = os.environ.get("WAITLIST_HOLD_DURATION_PARAMETER_NAME", "").strip()
    if not parameter_name:
        raise RuntimeError("WAITLIST_HOLD_DURATION_PARAMETER_NAME is not configured.")
    value = int(get_ssm_parameter(parameter_name, decrypt=False))
    if value <= 0:
        raise RuntimeError("Waitlist hold duration must be greater than zero.")
    return value


def query_all_items(table: Any, **query_args: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    while True:
        page = table.query(**query_args)
        items.extend(page.get("Items", []))
        last_key = page.get("LastEvaluatedKey")
        if not last_key:
            return items
        query_args["ExclusiveStartKey"] = last_key


def hold_slot_seat(slot: dict[str, Any]) -> bool:
    table = get_slots_table()
    try:
        table.update_item(
            Key=slot_table_key(slot),
            UpdateExpression="SET held_count = if_not_exists(held_count, :zero) + :one",
            ConditionExpression=(
                "is_active = :active AND #capacity > (booked_count + if_not_exists(held_count, :zero))"
            ),
            ExpressionAttributeNames={"#capacity": "capacity"},
            ExpressionAttributeValues={":one": 1, ":zero": 0, ":active": True},
        )
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise


def join_waitlist(patient_id: str, slot_id: str) -> tuple[int, dict[str, Any]]:
    slot = get_slot_by_id(slot_id)
    state = slot_availability_state(slot)
    if state == "missing":
        return 404, {"message": "Slot not found."}
    if state == "past":
        return 409, {"message": "Past slots cannot be waitlisted."}
    if state != "full":
        return 409, {"message": "Waitlists are available only when the slot is full."}

    now = int(time.time())
    joined_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    entry = {
        "slot_ref": slot_id,
        "patient_id": patient_id,
        "slot_start": str(slot["start_time"]),
        "consultation_type_id": str(slot["consultation_type_id"]),
        "joined_at": joined_at,
        "expires_at": int(slot["expires_at"] or 0),
    }
    try:
        waitlist_table().put_item(
            Item=entry,
            ConditionExpression="attribute_not_exists(slot_ref)",
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return 200, {"message": "You are already on this waitlist.", "joined_at": joined_at}
        raise

    count = len(query_all_items(
        waitlist_table(),
        IndexName="gsi_patient",
        KeyConditionExpression=Key("patient_id").eq(patient_id) & Key("slot_start").gt(""),
    ))
    if count > 5:
        waitlist_table().delete_item(Key={"slot_ref": slot_id, "patient_id": patient_id})
        return 409, {"message": "A patient can join at most five waitlists."}
    return 201, {"message": "You joined the waitlist.", "joined_at": joined_at}


def remove_waitlist(patient_id: str, slot_id: str) -> tuple[int, dict[str, Any]]:
    response = waitlist_table().delete_item(
        Key={"slot_ref": slot_id, "patient_id": patient_id},
        ConditionExpression="attribute_exists(slot_ref)",
        ReturnValues="ALL_OLD",
    )
    if not response.get("Attributes"):
        return 404, {"message": "Waitlist entry not found."}
    return 200, {"message": "You left the waitlist.", "slot_id": slot_id}


def list_waitlists(patient_id: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    waitlists = query_all_items(
        waitlist_table(),
        IndexName="gsi_patient",
        KeyConditionExpression=Key("patient_id").eq(patient_id) & Key("slot_start").gt(now),
    )
    offers = query_all_items(
        waitlist_offer_table(),
        IndexName="gsi_patient_offer",
        KeyConditionExpression=Key("patient_id").eq(patient_id) & Key("expires_at").gte(int(time.time())),
    )
    return {"waitlists": waitlists, "offers": offers}


def promote_waitlist(slot: dict[str, Any], *, seat_already_held: bool = False) -> dict[str, Any] | None:
    if not seat_already_held and not hold_slot_seat(slot):
        return None

    queue = query_all_items(
        waitlist_table(),
        IndexName="lsi_queue",
        KeyConditionExpression=Key("slot_ref").eq(str(slot["slot_id"])),
        ScanIndexForward=True,
    )
    for entry in queue:
        patient_id = str(entry["patient_id"])
        if int(entry.get("expires_at") or 0) <= int(time.time()):
            waitlist_table().delete_item(Key={"slot_ref": str(slot["slot_id"]), "patient_id": patient_id})
            continue
        now = int(time.time())
        expires_at = now + waitlist_hold_seconds()
        offer_id = str(uuid.uuid4())
        offer = {
            "slot_ref": str(slot["slot_id"]),
            "patient_id": patient_id,
            "offer_id": offer_id,
            "slot_start": str(slot["start_time"]),
            "consultation_type_id": str(slot["consultation_type_id"]),
            "expires_at": expires_at,
            "live_shard": "ACTIVE",
            "status": "OPEN",
            "created_at": now,
        }
        try:
            waitlist_offer_table().put_item(Item=offer, ConditionExpression="attribute_not_exists(slot_ref)")
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code == "ConditionalCheckFailedException":
                return None
            raise

        waitlist_table().delete_item(Key={"slot_ref": str(slot["slot_id"]), "patient_id": patient_id})
        contacts = execute_query(
            """SELECT u.user_id, u.email FROM patients p JOIN users u ON u.user_id = p.user_id
               WHERE p.patient_id = %s AND u.is_active = TRUE LIMIT 1""",
            (patient_id,),
        )
        if contacts:
            publish_domain_event("WaitlistOfferIssued", {
                "event_key": offer_id,
                "appointment_id": offer_id,
                "consultation_type_id": str(slot["consultation_type_id"]),
                "appointment_datetime": str(slot["start_time"]),
                "message": f"A seat opened up. Accept before {datetime.fromtimestamp(expires_at, timezone.utc).isoformat()}.",
                "recipients": [{"user_id": str(contacts[0]["user_id"]), "email": str(contacts[0]["email"]), "role": "patient"}],
            }, source="medislot.waitlist")
        return offer

    release_slot_seat(slot, held=True)
    return None


def expire_waitlist_offers() -> int:
    now = int(time.time())
    expired = query_all_items(
        waitlist_offer_table(),
        IndexName="gsi_live",
        KeyConditionExpression=Key("live_shard").eq("ACTIVE") & Key("expires_at").lte(now),
    )
    processed = 0
    for offer in expired:
        try:
            waitlist_offer_table().update_item(
                Key={"slot_ref": offer["slot_ref"], "patient_id": offer["patient_id"]},
                UpdateExpression="SET #status = :expired REMOVE live_shard",
                ConditionExpression="#status = :open AND expires_at <= :now",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":expired": "EXPIRED", ":open": "OPEN", ":now": now},
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                continue
            raise
        publish_domain_event("WaitlistOfferExpired", {
            "slot_id": str(offer["slot_ref"]), "seat_held": True,
        }, source="medislot.waitlist")
        processed += 1
    return processed


def accept_waitlist_offer(patient_id: str, offer_id: str) -> tuple[int, dict[str, Any]]:
    offers = query_all_items(
        waitlist_offer_table(),
        IndexName="gsi_patient_offer",
        KeyConditionExpression=Key("patient_id").eq(patient_id) & Key("expires_at").gte(int(time.time())),
    )
    offer = next((item for item in offers if item.get("offer_id") == offer_id and item.get("status") == "OPEN"), None)
    if not offer:
        return 404, {"message": "Active waitlist offer not found."}

    slot = get_slot_by_id(str(offer["slot_ref"]))
    if not slot:
        return 409, {"message": "The offered slot is no longer available."}

    try:
        convert_held_seat_to_booked(slot)
    except Exception:
        logger.exception("Could not convert held seat for offer %s.", offer_id)
        return 409, {"message": "The offered seat is no longer available."}

    appointment_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"waitlist:{offer_id}"))
    try:
        with database_transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """INSERT INTO appointments
                       (appointment_id, patient_id, doctor_id, consultation_type_id, appointment_datetime, appointment_status)
                       VALUES (%s, %s, %s, %s, %s, 'CONFIRMED')""",
                    (
                        appointment_id,
                        patient_id,
                        str(slot["doctor_id"]),
                        str(offer["consultation_type_id"]),
                        str(slot["start_time"]),
                    ),
                )
                consultation = execute_query(
                    "SELECT fee FROM consultation_types WHERE consultation_type_id = %s LIMIT 1",
                    (offer["consultation_type_id"],),
                )
                fee = consultation[0]["fee"] if consultation else 0
                if fee > 0:
                    cursor.execute(
                        """INSERT INTO payments
                           (payment_id, appointment_id, amount, payment_status, payment_method)
                           VALUES (%s, %s, %s, 'INITIATED', 'PAY_AT_CLINIC')""",
                        (str(uuid.uuid4()), appointment_id, fee),
                    )
        waitlist_offer_table().update_item(
            Key={"slot_ref": offer["slot_ref"], "patient_id": patient_id},
            UpdateExpression="SET #status = :accepted REMOVE live_shard",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":accepted": "ACCEPTED"},
        )
        publish_domain_event("AppointmentBooked", {
            "event_key": appointment_id,
            "appointment_id": appointment_id,
            "consultation_type_id": str(offer["consultation_type_id"]),
            "appointment_datetime": str(slot["start_time"]),
            "message": "Your waitlist offer was accepted.",
            "recipients": [],
        })
    except Exception:
        logger.exception("Waitlist offer acceptance failed for %s.", offer_id)
        release_slot_seat(slot, held=False)
        hold_slot_seat(slot)
        return 500, {"message": "Could not accept the waitlist offer. Please retry."}

    return 201, {"appointment_id": appointment_id, "status": "CONFIRMED"}
