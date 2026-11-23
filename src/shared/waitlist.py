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

from read_api.service import hold_slot_seat, release_slot_seat, slot_availability_state
from shared.config import get_ssm_parameter
from shared.database import execute_query
from shared.notifications import publish_email_notification


logger = logging.getLogger(__name__)
_dynamodb = None


def _table(environment_name: str) -> Any:
    global _dynamodb
    table_name = os.environ.get(environment_name, "").strip()
    if not table_name:
        raise RuntimeError(f"{environment_name} is not configured.")
    if _dynamodb is None:
        _dynamodb = boto3.resource(
            "dynamodb",
            region_name=os.environ.get("AWS_REGION", "ap-south-1"),
        )
    return _dynamodb.Table(table_name)


def waitlist_table() -> Any:
    return _table("WAITLIST_TABLE")


def waitlist_offer_table() -> Any:
    return _table("WAITLIST_OFFER_TABLE")


def waitlist_hold_seconds() -> int:
    parameter_name = os.environ.get("WAITLIST_HOLD_DURATION_PARAMETER_NAME", "").strip()
    if not parameter_name:
        raise RuntimeError("WAITLIST_HOLD_DURATION_PARAMETER_NAME is not configured.")
    try:
        hold_seconds = int(get_ssm_parameter(parameter_name, decrypt=False))
    except ValueError as exc:
        raise RuntimeError("Waitlist hold duration in SSM must be an integer.") from exc
    if hold_seconds <= 0:
        raise RuntimeError("Waitlist hold duration must be greater than zero.")
    return hold_seconds


def query_all_items(table: Any, **query_args: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    while True:
        page = table.query(**query_args)
        items.extend(page.get("Items", []))
        last_key = page.get("LastEvaluatedKey")
        if not last_key:
            return items
        query_args["ExclusiveStartKey"] = last_key


def notify_waitlist_offer(
    patient_id: str,
    offer_id: str,
    slot: dict[str, Any],
    expires_at: int,
    event_type: str,
) -> None:
    contacts = execute_query(
        """
        SELECT u.user_id, u.email
        FROM patients p
        JOIN users u ON u.user_id = p.user_id
        WHERE p.patient_id = %s AND u.is_active = TRUE
        LIMIT 1
        """,
        (patient_id,),
    )
    if not contacts:
        logger.error("Could not resolve waitlist notification recipient for patient %s.", patient_id)
        return
    publish_email_notification(
        event_key=f"{offer_id}#{event_type}",
        event_type=event_type,
        recipient_user_id=str(contacts[0]["user_id"]),
        recipient_email=str(contacts[0]["email"]),
        subject="MediSlot waitlist slot update",
        message=(
            f"{event_type.replace('_', ' ').title()} for slot "
            f"{slot.get('start_time')}. Offer expires at "
            f"{datetime.fromtimestamp(expires_at, timezone.utc).isoformat()}."
        ),
    )


def promote_waitlist(
    slot: dict[str, Any],
    *,
    seat_already_held: bool = False,
) -> dict[str, Any] | None:
    """Offer one held seat to the earliest active waiter, or release it."""
    state = slot_availability_state(slot)
    if state in {"missing", "inactive", "past", "no_capacity"}:
        if seat_already_held:
            release_slot_seat(slot, held=True)
        return None
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
            waitlist_table().delete_item(
                Key={"slot_ref": str(slot["slot_id"]), "patient_id": patient_id}
            )
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
            waitlist_offer_table().put_item(
                Item=offer,
                ConditionExpression="attribute_not_exists(slot_ref)",
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                if not seat_already_held:
                    release_slot_seat(slot, held=True)
                return None
            if not seat_already_held:
                release_slot_seat(slot, held=True)
            raise

        waitlist_table().delete_item(
            Key={"slot_ref": str(slot["slot_id"]), "patient_id": patient_id}
        )
        try:
            notify_waitlist_offer(
                patient_id,
                offer_id,
                slot,
                expires_at,
                "WAITLIST_HOLD_OFFER_ISSUED",
            )
        except Exception:
            logger.exception("Waitlist offer %s was created but notification failed.", offer_id)
        return offer

    release_slot_seat(slot, held=True)
    return None
