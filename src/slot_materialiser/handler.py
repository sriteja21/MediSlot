"""
MediSlot Slot Materialiser Lambda

Generates appointment slots for the rolling 30-day window [tomorrow, today + 30 days]
based on active availability patterns and consultation types, excluding blackout dates.
Slots are written idempotently into DynamoDB with ConditionExpression=Attr('slot_id').not_exists().
"""

import logging
import os
import sys
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

# Ensure src root is in sys.path so shared imports work
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from shared.database import execute_query

# ============================================================
# LOGGING & CONFIGURATION
# ============================================================

logger = logging.getLogger()
logger.setLevel(logging.INFO)

AWS_REGION = os.environ.get("AWS_REGION", "ap-south-1")
ENVIRONMENT = os.environ.get("ENVIRONMENT", "dev")

SLOTS_TABLE_NAME = (
    os.environ.get("SLOTS_TABLE_NAME")
    or os.environ.get("SLOTS_TABLE")
    or f"MediSlot-Slots-{ENVIRONMENT}"
)

dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
slots_table = dynamodb.Table(SLOTS_TABLE_NAME)


# ============================================================
# TIME HELPERS
# ============================================================

def to_time_object(val: Any) -> time:
    """Normalize MySQL TIME value (timedelta, time, or str) into datetime.time."""
    if isinstance(val, time):
        return val
    if isinstance(val, timedelta):
        total_seconds = int(val.total_seconds())
        hours = (total_seconds // 3600) % 24
        minutes = (total_seconds % 3600) // 60
        seconds = total_seconds % 60
        return time(hours, minutes, seconds)
    if isinstance(val, str):
        parts = [int(p) for p in val.strip().split(":")]
        return time(parts[0], parts[1], parts[2] if len(parts) > 2 else 0)
    raise ValueError(f"Unsupported time type: {type(val)}: {val}")


def parse_date(val: Any) -> date:
    """Normalize a database DATE representation into a datetime.date object."""
    if isinstance(val, date):
        return val
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, str):
        return datetime.strptime(val.strip(), "%Y-%m-%d").date()
    raise ValueError(f"Unsupported date format: {type(val)}: {val}")


def time_ranges_overlap(start_a: time, end_a: time, start_b: time, end_b: time) -> bool:
    """Return True when two time windows overlap on the same day."""
    return start_a < end_b and start_b < end_a


def to_utc_datetime(value: Any) -> datetime:
    """Normalize a DynamoDB ISO timestamp to an aware UTC datetime."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def query_doctor_slots(
    doctor_id: str,
    utc_start: datetime,
    utc_end: datetime,
) -> list[dict[str, Any]]:
    """Return every slot for a doctor in a UTC time range."""
    query_args: dict[str, Any] = {
        "IndexName": "gsi_doctor_time",
        "KeyConditionExpression": (
            Key("doctor_id").eq(doctor_id)
            & Key("start_time").between(
                utc_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                (utc_end - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
        ),
    }
    slots: list[dict[str, Any]] = []
    while True:
        result = slots_table.query(**query_args)
        slots.extend(result.get("Items", []))
        last_key = result.get("LastEvaluatedKey")
        if not last_key:
            return slots
        query_args["ExclusiveStartKey"] = last_key


def update_unbooked_slot(
    slot_id: str,
    *,
    is_active: bool,
    is_blocked: bool,
    inactive_reason: str,
    capacity: int | None = None,
    end_time: str | None = None,
) -> bool:
    """Update a slot only if it has no booked seats."""
    expressions = [
        "is_active = :is_active",
        "is_blocked = :is_blocked",
        "inactive_reason = :inactive_reason",
    ]
    attribute_names: dict[str, str] = {}
    values: dict[str, Any] = {
        ":is_active": is_active,
        ":is_blocked": is_blocked,
        ":inactive_reason": inactive_reason,
        ":zero": 0,
    }
    if capacity is not None:
        expressions.append("#capacity = :capacity")
        attribute_names["#capacity"] = "capacity"
        values[":capacity"] = capacity
    if end_time is not None:
        expressions.append("end_time = :end_time")
        values[":end_time"] = end_time

    update_args: dict[str, Any] = {
        "Key": {"slot_id": slot_id},
        "UpdateExpression": "SET " + ", ".join(expressions),
        "ConditionExpression": (
            "attribute_not_exists(booked_count) OR booked_count = :zero"
        ),
        "ExpressionAttributeValues": values,
    }
    if attribute_names:
        update_args["ExpressionAttributeNames"] = attribute_names

    try:
        slots_table.update_item(**update_args)
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise


# ============================================================
# HANDLER
# ============================================================

def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    Main Lambda entrypoint.

    Materialises slots for the next 30 local calendar days:
    1. Determine each doctor's rolling window [local tomorrow, local today + 30 days].
     2. Generate desired slots from active weekly patterns, marking blackout
         slots inactive instead of omitting them.
     3. Reconcile existing slots in the same window, changing only unbooked items.
    """
    logger.info("Starting slot materialisation run. table=%s environment=%s", SLOTS_TABLE_NAME, ENVIRONMENT)

    now_utc = datetime.now(timezone.utc)
    fallback_start_date = now_utc.date() + timedelta(days=1)
    fallback_end_date = now_utc.date() + timedelta(days=30)

    # Query doctors separately so doctors with no remaining patterns are reconciled.
    doctors = execute_query(
        """
        SELECT d.doctor_id, d.timezone
        FROM doctors d
        JOIN users u ON u.user_id = d.user_id
        WHERE u.is_active = TRUE
        """
    )
    query_patterns = """
        SELECT
            ct.doctor_id,
            ap.pattern_id AS pattern_id,
            ap.day_of_week,
            ap.start_time AS start_local_time,
            ap.end_time AS end_local_time,
            ct.consultation_type_id AS consultation_type_id,
            catalog.duration_minutes,
            ct.capacity
        FROM availability_patterns ap
        JOIN consultation_types ct
            ON ct.consultation_type_id = ap.consultation_type_id
        JOIN consultation_catalog catalog
            ON catalog.consultation_type_id = ct.catalog_type_id
        WHERE ap.is_active = TRUE AND ct.is_active = TRUE
        ORDER BY ct.doctor_id, ap.day_of_week, ap.start_time
    """
    patterns = execute_query(query_patterns)
    logger.info("Retrieved %d active availability patterns from database", len(patterns))

    # Group patterns by doctor_id
    doctor_patterns: dict[str, list[dict[str, Any]]] = {}
    for p in patterns:
        doc_id = str(p["doctor_id"])
        doctor_patterns.setdefault(doc_id, []).append(p)

    doctor_windows: dict[
        str, tuple[ZoneInfo, list[date], datetime, datetime]
    ] = {}
    window_start_dates: list[date] = []
    window_end_dates: list[date] = []
    for doctor in doctors:
        doc_id = str(doctor["doctor_id"])
        try:
            doctor_timezone = ZoneInfo(str(doctor.get("timezone") or "UTC"))
        except Exception:
            logger.exception("Invalid timezone for doctor %s; skipping doctor", doc_id)
            continue

        local_today = now_utc.astimezone(doctor_timezone).date()
        local_start_date = local_today + timedelta(days=1)
        local_end_date = local_today + timedelta(days=30)
        dates_in_window = [
            local_start_date + timedelta(days=offset)
            for offset in range(30)
        ]
        utc_start = datetime.combine(
            local_start_date, time.min, tzinfo=doctor_timezone
        ).astimezone(timezone.utc)
        utc_end = datetime.combine(
            local_end_date + timedelta(days=1), time.min, tzinfo=doctor_timezone
        ).astimezone(timezone.utc)
        doctor_windows[doc_id] = (
            doctor_timezone,
            dates_in_window,
            utc_start,
            utc_end,
        )
        window_start_dates.append(local_start_date)
        window_end_dates.append(local_end_date)

    if not doctor_windows:
        return {
            "statusCode": 200,
            "body": {
                "message": "No valid doctor timezones to process.",
                "window_start": fallback_start_date.isoformat(),
                "window_end": fallback_end_date.isoformat(),
                "total_generated": 0,
                "total_skipped": 0,
            },
        }

    window_start = min(window_start_dates, default=fallback_start_date)
    window_end = max(window_end_dates, default=fallback_end_date)
    logger.info(
        "Materialisation local-date range across doctors: %s to %s (inclusive)",
        window_start.isoformat(),
        window_end.isoformat(),
    )

    # 2. Query blackout ranges for these doctors within the window
    doctor_ids = list(doctor_windows.keys())
    if not doctor_ids:
        return {
            "statusCode": 200,
            "body": {
                "message": "No active doctors to materialise.",
                "window_start": window_start.isoformat(),
                "window_end": window_end.isoformat(),
                "total_generated": 0,
                "total_updated": 0,
                "total_deactivated": 0,
                "total_skipped": 0,
            },
        }
    blackouts_by_doctor: dict[str, list[tuple[datetime, datetime]]] = {
        doc_id: [] for doc_id in doctor_ids
    }

    window_start_utc = min(
        window[2] for window in doctor_windows.values()
    )
    window_end_utc = max(
        window[3] for window in doctor_windows.values()
    )

    # Query blackouts in batches or using IN clause
    format_strings = ",".join(["%s"] * len(doctor_ids))
    query_blackouts = f"""
        SELECT doctor_id, start_datetime, end_datetime
        FROM blackout_dates
        WHERE doctor_id IN ({format_strings})
          AND start_datetime < %s
          AND end_datetime > %s
    """
    params = tuple(doctor_ids) + (
        window_end_utc.replace(tzinfo=None),
        window_start_utc.replace(tzinfo=None),
    )
    blackout_rows = execute_query(query_blackouts, params)

    for row in blackout_rows:
        d_id = str(row["doctor_id"])
        blackout_start = row["start_datetime"]
        blackout_end = row["end_datetime"]
        if blackout_start.tzinfo is None:
            blackout_start = blackout_start.replace(tzinfo=timezone.utc)
        if blackout_end.tzinfo is None:
            blackout_end = blackout_end.replace(tzinfo=timezone.utc)
        if d_id in blackouts_by_doctor:
            blackouts_by_doctor[d_id].append((blackout_start, blackout_end))

    logger.info("Retrieved %d blackout date entries for active doctors in window", len(blackout_rows))

    existing_by_doctor: dict[str, dict[str, dict[str, Any]]] = {}
    booked_ranges_by_doctor: dict[str, list[tuple[str, datetime, datetime]]] = {}
    for doc_id, (_, _, utc_start, utc_end) in doctor_windows.items():
        existing_slots = query_doctor_slots(doc_id, utc_start, utc_end)
        existing_by_doctor[doc_id] = {
            str(item["slot_id"]): item for item in existing_slots
        }
        booked_ranges_by_doctor[doc_id] = []
        for item in existing_slots:
            if int(item.get("booked_count", 0) or 0) <= 0:
                continue
            booked_ranges_by_doctor[doc_id].append(
                (
                    str(item["slot_id"]),
                    to_utc_datetime(item["start_time"]),
                    to_utc_datetime(item["end_time"]),
                )
            )

    total_generated = 0
    total_updated = 0
    total_deactivated = 0
    total_skipped = 0
    generated_day_ranges: dict[tuple[str, date], list[tuple[datetime, datetime]]] = {}
    desired_slots: dict[str, dict[str, Any]] = {}

    # 3. Build the desired slot set, including slots falling under blackouts.
    for doc_id, (doctor_timezone, dates_in_window, _, _) in doctor_windows.items():
        doc_pattern_list = doctor_patterns.get(doc_id, [])
        doc_blackouts = blackouts_by_doctor.get(doc_id, [])
        booked_ranges = booked_ranges_by_doctor.get(doc_id, [])

        for p in doc_pattern_list:
            pattern_dow = int(p["day_of_week"])  # 0=Sun, ..., 6=Sat
            duration_minutes = int(p["duration_minutes"])
            capacity = int(p["capacity"])
            consultation_type_id = str(p["consultation_type_id"])

            if duration_minutes <= 0:
                logger.warning(
                    "Invalid duration_minutes (%d) for consultation type %s; skipping pattern %s",
                    duration_minutes,
                    consultation_type_id,
                    p["pattern_id"],
                )
                continue

            try:
                start_t = to_time_object(p["start_local_time"])
                end_t = to_time_object(p["end_local_time"])
            except Exception as e:
                logger.error("Failed to parse pattern times for pattern %s: %s", p["pattern_id"], e)
                continue

            step = timedelta(minutes=duration_minutes)

            for target_date in dates_in_window:
                # Translate Python weekday (Mon=0) to schema weekday (Sun=0).
                if (target_date.weekday() + 1) % 7 != pattern_dow:
                    continue

                day_key = (doc_id, target_date)
                day_ranges = generated_day_ranges.setdefault(day_key, [])
                pattern_start = datetime.combine(target_date, start_t, tzinfo=doctor_timezone)
                pattern_end = datetime.combine(target_date, end_t, tzinfo=doctor_timezone)

                for existing_start, existing_end in day_ranges:
                    if time_ranges_overlap(pattern_start.time(), pattern_end.time(), existing_start.time(), existing_end.time()):
                        logger.warning(
                            "Skipping overlapping availability for doctor=%s date=%s existing=[%s,%s] incoming=[%s,%s]",
                            doc_id,
                            target_date.isoformat(),
                            existing_start.strftime("%H:%M:%S"),
                            existing_end.strftime("%H:%M:%S"),
                            pattern_start.strftime("%H:%M:%S"),
                            pattern_end.strftime("%H:%M:%S"),
                        )
                        total_skipped += 1
                        break
                else:
                    slot_start_dt = pattern_start
                    while slot_start_dt + step <= pattern_end:
                        slot_end_dt = slot_start_dt + step
                        slot_start_utc = slot_start_dt.astimezone(timezone.utc)
                        slot_end_utc = slot_end_dt.astimezone(timezone.utc)
                        is_blackout = any(
                            slot_start_utc < blackout_end
                            and blackout_start < slot_end_utc
                            for blackout_start, blackout_end in doc_blackouts
                        )
                        candidate_overlaps = False
                        for existing_start, existing_end in day_ranges:
                            if slot_start_dt < existing_end and existing_start < slot_end_dt:
                                logger.warning(
                                    "Skipping overlapping generated slot for doctor=%s date=%s slot_start=%s slot_end=%s existing_start=%s existing_end=%s",
                                    doc_id,
                                    target_date.isoformat(),
                                    slot_start_dt.isoformat(),
                                    slot_end_dt.isoformat(),
                                    existing_start.isoformat(),
                                    existing_end.isoformat(),
                                )
                                candidate_overlaps = True
                                break
                        if candidate_overlaps:
                            slot_start_dt = slot_end_dt
                            continue

                        start_utc = slot_start_utc
                        end_utc = slot_end_utc
                        start_epoch = int(start_utc.timestamp())
                        slot_id = f"{consultation_type_id}#{start_epoch}"
                        booked_overlap = any(
                            booked_slot_id != slot_id
                            and slot_start_utc < booked_end
                            and booked_start < slot_end_utc
                            for booked_slot_id, booked_start, booked_end in booked_ranges
                        )
                        is_blocked = is_blackout or booked_overlap
                        inactive_reason = (
                            "BLACKOUT"
                            if is_blackout
                            else "BOOKED_OVERLAP"
                            if booked_overlap
                            else "NONE"
                        )
                        expires_at = start_epoch + (7 * 86400)  # TTL: start_epoch + 7 days

                        start_iso = start_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
                        end_iso = end_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

                        item = {
                            "slot_id": slot_id,
                            "doctor_id": doc_id,
                            "consultation_type_id": consultation_type_id,
                            "start_time": start_iso,
                            "end_time": end_iso,
                            "capacity": capacity,
                            "booked_count": 0,
                            "is_active": not is_blocked,
                            "is_blocked": is_blocked,
                            "inactive_reason": inactive_reason,
                            "expires_at": expires_at,
                        }
                        desired_slots[slot_id] = item

                        day_ranges.append((slot_start_dt, slot_end_dt))
                        slot_start_dt = slot_end_dt

    # 4. Reconcile generated slots against DynamoDB without modifying bookings.
    for doc_id, (_, _, utc_start, utc_end) in doctor_windows.items():
        existing_by_id = existing_by_doctor[doc_id]
        desired_for_doctor = {
            slot_id: item
            for slot_id, item in desired_slots.items()
            if item["doctor_id"] == doc_id
        }

        for slot_id, item in desired_for_doctor.items():
            existing = existing_by_id.get(slot_id)
            if existing is None:
                try:
                    slots_table.put_item(
                        Item=item,
                        ConditionExpression=Attr("slot_id").not_exists(),
                    )
                    total_generated += 1
                except ClientError as exc:
                    if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                        total_skipped += 1
                    else:
                        logger.exception("Unexpected DynamoDB error writing slot %s", slot_id)
                        raise
                continue

            if int(existing.get("booked_count", 0) or 0) > 0:
                total_skipped += 1
                continue

            if update_unbooked_slot(
                slot_id,
                is_active=bool(item["is_active"]),
                is_blocked=bool(item["is_blocked"]),
                inactive_reason=str(item["inactive_reason"]),
                capacity=int(item["capacity"]),
                end_time=str(item["end_time"]),
            ):
                total_updated += 1
            else:
                total_skipped += 1

        for slot_id, existing in existing_by_id.items():
            if slot_id in desired_for_doctor:
                continue
            if int(existing.get("booked_count", 0) or 0) > 0:
                total_skipped += 1
                continue
            if update_unbooked_slot(
                slot_id,
                is_active=False,
                is_blocked=False,
                inactive_reason="AVAILABILITY_CHANGED",
            ):
                total_deactivated += 1
            else:
                total_skipped += 1

    logger.info(
        "Slot materialisation completed. generated=%d updated=%d deactivated=%d skipped=%d",
        total_generated,
        total_updated,
        total_deactivated,
        total_skipped,
    )

    return {
        "statusCode": 200,
        "body": {
            "message": "Slot materialisation completed successfully.",
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "total_generated": total_generated,
            "total_updated": total_updated,
            "total_deactivated": total_deactivated,
            "total_skipped": total_skipped,
        },
    }
