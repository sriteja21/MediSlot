"""Slot and consultation read repository shared across MediSlot APIs."""

import os
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import boto3
from botocore.exceptions import ClientError
from boto3.dynamodb.conditions import Key


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
    """Return the cached DynamoDB slots table."""
    global _dynamodb_resource, _slots_table
    if _slots_table is None:
        _dynamodb_resource = boto3.resource("dynamodb", region_name=AWS_REGION)
        _slots_table = _dynamodb_resource.Table(SLOTS_TABLE_NAME)
    return _slots_table


def query_doctor_slots(
    doctor_id: str,
    utc_start: datetime,
    utc_end: datetime,
    *,
    table: Any = None,
    include_end: bool = False,
) -> list[dict[str, Any]]:
    """Query every slot for a doctor in a UTC range, following all GSI pages."""
    start_key = to_utc_datetime(utc_start).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = to_utc_datetime(utc_end)
    if not include_end:
        end -= timedelta(seconds=1)
    end_key = end.strftime("%Y-%m-%dT%H:%M:%SZ")
    query_args: dict[str, Any] = {
        "IndexName": "gsi_doctor_time",
        "KeyConditionExpression": (
            Key("doctor_id").eq(doctor_id)
            & Key("start_time").between(start_key, end_key)
        ),
    }
    results: list[dict[str, Any]] = []
    slot_table = table if table is not None else get_slots_table()
    while True:
        page = slot_table.query(**query_args)
        results.extend(page.get("Items", []))
        last_key = page.get("LastEvaluatedKey")
        if not last_key:
            return results
        query_args["ExclusiveStartKey"] = last_key


def get_slot_by_id(slot_id: str, *, table: Any = None) -> dict[str, Any] | None:
    """Fetch a single slot using the table's consultation-type/time index."""
    try:
        consultation_type_id, raw_epoch = slot_id.rsplit("#", 1)
        start_time = datetime.fromtimestamp(int(raw_epoch), timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    except (ValueError, TypeError):
        return None
    slot_table = table if table is not None else get_slots_table()
    query_args: dict[str, Any] = {
        "IndexName": "gsi_type_time",
        "KeyConditionExpression": (
            Key("consultation_type_id").eq(consultation_type_id)
            & Key("start_time").eq(start_time)
        ),
    }
    while True:
        page = slot_table.query(**query_args)
        for item in page.get("Items", []):
            if item.get("slot_id") == slot_id:
                return item
        last_key = page.get("LastEvaluatedKey")
        if not last_key:
            return None
        query_args["ExclusiveStartKey"] = last_key


def free_seats(capacity: Any, booked_count: Any, held_count: Any = 0) -> int:
    """Calculate unoccupied seats using capacity minus booked and held seats."""
    return int(capacity or 0) - int(booked_count or 0) - int(held_count or 0)


def slot_free_seats(slot: dict[str, Any]) -> int:
    """Return the number of unoccupied seats in a slot item."""
    return free_seats(
        slot.get("capacity"),
        slot.get("booked_count"),
        slot.get("held_count"),
    )


def slot_belongs_to_doctor(slot: dict[str, Any] | None, doctor_id: str) -> bool:
    """Return whether a fetched slot belongs to the given doctor."""
    return bool(slot and slot.get("doctor_id") == doctor_id)


def slot_is_available(
    slot: dict[str, Any] | None,
    *,
    now: datetime | None = None,
) -> bool:
    """Return whether a slot is active, in the future, and has a free seat."""
    return slot_availability_state(slot, now=now) == "available"


def slot_availability_state(
    slot: dict[str, Any] | None,
    *,
    now: datetime | None = None,
) -> str:
    """Classify a slot for shared availability filtering and error responses."""
    if not slot:
        return "missing"
    if (
        slot.get("is_active") is False
        or slot.get("is_blocked")
        or str(slot.get("inactive_reason") or "").upper() == "BLACKOUT"
    ):
        return "inactive"
    start_time = slot.get("start_time")
    if not start_time:
        return "past"
    try:
        parsed_start = datetime.fromisoformat(str(start_time).replace("Z", "+00:00"))
    except ValueError:
        return "past"
    if parsed_start.tzinfo is None:
        parsed_start = parsed_start.replace(tzinfo=timezone.utc)
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    if parsed_start.astimezone(timezone.utc) <= current_time.astimezone(timezone.utc):
        return "past"
    if int(slot.get("capacity") or 0) <= 0:
        return "no_capacity"
    return "available" if slot_free_seats(slot) > 0 else "full"


def slot_id_for_datetime(consultation_type_id: str, appointment_datetime: datetime) -> str:
    """Build the deterministic slot ID for a consultation type and UTC start."""
    return f"{consultation_type_id}#{int(appointment_datetime.timestamp())}"


def slot_sort_key(slot_id: str) -> str:
    """Build the DynamoDB range key expected by the existing Slots table."""
    consultation_type_id, raw_epoch = slot_id.rsplit("#", 1)
    return f"{int(raw_epoch)}#{consultation_type_id}"


def slot_table_key(slot: dict[str, Any]) -> dict[str, str]:
    """Build the existing Slots table primary key from a slot item."""
    return {
        "doctor_id": str(slot["doctor_id"]),
        "slot_sk": str(slot.get("slot_sk") or slot_sort_key(str(slot["slot_id"]))),
    }


def to_utc_datetime(value: Any) -> datetime:
    """Normalize a timestamp value to an aware UTC datetime."""
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def local_day_utc_bounds(day: date, timezone_name: str) -> tuple[datetime, datetime]:
    """Convert the start and end of a doctor-local date into UTC."""
    clean_timezone = timezone_name or "UTC"
    local_timezone = timezone.utc if clean_timezone == "UTC" else ZoneInfo(clean_timezone)
    local_start = datetime.combine(day, time.min, tzinfo=local_timezone)
    local_end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=local_timezone)
    return local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)


def slot_response(slot: dict[str, Any]) -> dict[str, Any]:
    """Preserve slot fields while normalizing timestamps and adding free seats."""
    response = dict(slot)
    response["free_seats"] = slot_free_seats(slot)
    for field in ("start_time", "end_time"):
        value = response.get(field)
        if not value or (isinstance(value, str) and value.endswith("Z")):
            continue
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        response[field] = (
            parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        )
    return response


def reserve_slot_seat(slot: dict[str, Any], capacity: int, *, table: Any = None) -> None:
    """Atomically reserve one booked seat if the slot still has capacity."""
    slot_table = table if table is not None else get_slots_table()
    held_count = int(slot.get("held_count") or 0)
    if held_count:
        condition = (
            "is_active = :active AND "
            "(attribute_not_exists(booked_count) OR booked_count < :remaining_capacity) "
            "AND held_count = :held_count"
        )
        values = {
            ":remaining_capacity": free_seats(capacity, 0, held_count),
            ":held_count": held_count,
            ":active": True,
            ":inc": 1,
            ":zero": 0,
        }
    else:
        condition = (
            "is_active = :active AND "
            "(attribute_not_exists(booked_count) OR booked_count < :capacity) "
            "AND (attribute_not_exists(held_count) OR held_count = :zero)"
        )
        values = {
            ":capacity": capacity,
            ":active": True,
            ":inc": 1,
            ":zero": 0,
        }
    slot_table.update_item(
        Key=slot_table_key(slot),
        UpdateExpression="SET booked_count = if_not_exists(booked_count, :zero) + :inc",
        ConditionExpression=condition,
        ExpressionAttributeValues=values,
        ReturnValues="UPDATED_NEW",
    )


def release_slot_seat(
    slot: dict[str, Any],
    *,
    held: bool = False,
    table: Any = None,
) -> None:
    """Release one previously booked or held seat reservation."""
    slot_table = table if table is not None else get_slots_table()
    if held:
        slot_table.update_item(
            Key=slot_table_key(slot),
            UpdateExpression="SET held_count = held_count - :one",
            ConditionExpression="held_count > :zero",
            ExpressionAttributeValues={":one": 1, ":zero": 0},
        )
        return
    slot_table.update_item(
        Key=slot_table_key(slot),
        UpdateExpression="SET booked_count = booked_count - :one",
        ConditionExpression="booked_count > :zero",
        ExpressionAttributeValues={":one": 1, ":zero": 0},
    )


def hold_slot_seat(slot: dict[str, Any], *, table: Any = None) -> bool:
    """Reserve one free seat for an exclusive waitlist offer."""
    if slot_free_seats(slot) <= 0 or slot.get("is_active") is False:
        return False
    slot_table = table if table is not None else get_slots_table()
    booked_count = int(slot.get("booked_count") or 0)
    held_count = int(slot.get("held_count") or 0)
    condition = (
        "is_active = :active AND "
        "(attribute_not_exists(booked_count) OR booked_count = :booked) AND "
        "(attribute_not_exists(held_count) OR held_count = :held)"
    )
    try:
        slot_table.update_item(
            Key=slot_table_key(slot),
            UpdateExpression="SET held_count = if_not_exists(held_count, :zero) + :one",
            ConditionExpression=condition,
            ExpressionAttributeValues={
                ":active": True,
                ":booked": booked_count,
                ":held": held_count,
                ":zero": 0,
                ":one": 1,
            },
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise
    return True


def convert_booked_seat_to_hold(slot: dict[str, Any], *, table: Any = None) -> None:
    """Keep a cancelled booking's seat unavailable while its cancellation commits."""
    slot_table = table if table is not None else get_slots_table()
    slot_table.update_item(
        Key=slot_table_key(slot),
        UpdateExpression=(
            "SET booked_count = booked_count - :one, "
            "held_count = if_not_exists(held_count, :zero) + :one"
        ),
        ConditionExpression="booked_count > :zero",
        ExpressionAttributeValues={":one": 1, ":zero": 0},
    )


def convert_held_seat_to_booked(slot: dict[str, Any], *, table: Any = None) -> None:
    """Compensate a failed cancellation by restoring its seat reservation."""
    slot_table = table if table is not None else get_slots_table()
    slot_table.update_item(
        Key=slot_table_key(slot),
        UpdateExpression=(
            "SET booked_count = if_not_exists(booked_count, :zero) + :one "
            "ADD held_count :negative_one"
        ),
        ConditionExpression="held_count > :zero",
        ExpressionAttributeValues={
            ":zero": 0,
            ":one": 1,
            ":negative_one": -1,
        },
    )


def convert_held_seat_to_booking(
    slot: dict[str, Any],
    capacity: int,
    *,
    table: Any = None,
) -> None:
    """Atomically convert a waitlist hold into a booked seat."""
    slot_table = table if table is not None else get_slots_table()
    slot_table.update_item(
        Key=slot_table_key(slot),
        UpdateExpression=(
            "SET booked_count = if_not_exists(booked_count, :zero) + :one "
            "ADD held_count :negative_one"
        ),
        ConditionExpression=(
            "held_count > :zero AND "
            "(attribute_not_exists(booked_count) OR booked_count < :capacity)"
        ),
        ExpressionAttributeValues={
            ":zero": 0,
            ":one": 1,
            ":negative_one": -1,
            ":capacity": capacity,
        },
    )
