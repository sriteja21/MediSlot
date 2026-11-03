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
from boto3.dynamodb.conditions import Attr
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


# ============================================================
# HANDLER
# ============================================================

def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    Main Lambda entrypoint.

    Materialises slots for the next 30 local calendar days:
    1. Determine each doctor's rolling window [local tomorrow, local today + 30 days].
    2. Query active users, their active availability patterns and consultation types.
    3. Query blackout ranges for active doctors within the window.
    4. For each weekday matching pattern:
       - Generate slots from start_local_time to end_local_time in steps of duration_minutes.
         - Interpret availability times in the doctor's configured timezone.
         - Put item to DynamoDB with condition attribute_not_exists(slot_id).
    5. Log summary and return stats.
    """
    logger.info("Starting slot materialisation run. table=%s environment=%s", SLOTS_TABLE_NAME, ENVIRONMENT)

    now_utc = datetime.now(timezone.utc)
    fallback_start_date = now_utc.date() + timedelta(days=1)
    fallback_end_date = now_utc.date() + timedelta(days=30)

    # 1. Query active doctors with active patterns and consultation types
    query_patterns = """
        SELECT
            d.doctor_id AS doctor_id,
            d.timezone AS doctor_timezone,
            ap.pattern_id AS pattern_id,
            ap.day_of_week,
            ap.start_time AS start_local_time,
            ap.end_time AS end_local_time,
            ct.consultation_type_id AS consultation_type_id,
            ct.duration_minutes,
            ct.capacity
        FROM doctors d
        JOIN users u ON u.user_id = d.user_id AND u.is_active = TRUE
        JOIN consultation_types ct
            ON ct.doctor_id = d.doctor_id AND ct.is_active = TRUE
        JOIN availability_patterns ap
            ON ap.consultation_type_id = ct.consultation_type_id
            AND ap.is_active = TRUE
        ORDER BY d.doctor_id, ap.day_of_week, ap.start_time
    """
    patterns = execute_query(query_patterns)
    logger.info("Retrieved %d active availability patterns from database", len(patterns))

    if not patterns:
        logger.info("No active availability patterns found. Completed materialisation.")
        return {
            "statusCode": 200,
            "body": {
                "message": "No active availability patterns to process.",
                "window_start": fallback_start_date.isoformat(),
                "window_end": fallback_end_date.isoformat(),
                "total_generated": 0,
                "total_skipped": 0,
            },
        }

    # Group patterns by doctor_id
    doctor_patterns: dict[str, list[dict[str, Any]]] = {}
    for p in patterns:
        doc_id = str(p["doctor_id"])
        doctor_patterns.setdefault(doc_id, []).append(p)

    doctor_windows: dict[str, tuple[Any, list[date]]] = {}
    window_start_dates: list[date] = []
    window_end_dates: list[date] = []
    for doc_id, doc_pattern_list in doctor_patterns.items():
        try:
            doctor_timezone = ZoneInfo(
                str(doc_pattern_list[0].get("doctor_timezone") or "UTC")
            )
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
        doctor_windows[doc_id] = (doctor_timezone, dates_in_window)
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

    window_start = min(window_start_dates)
    window_end = max(window_end_dates)
    logger.info(
        "Materialisation local-date range across doctors: %s to %s (inclusive)",
        window_start.isoformat(),
        window_end.isoformat(),
    )

    # 2. Query blackout ranges for these doctors within the window
    doctor_ids = list(doctor_windows.keys())
    blackouts_by_doctor: dict[str, list[tuple[datetime, datetime]]] = {
        doc_id: [] for doc_id in doctor_ids
    }

    window_start_utc = min(
        datetime.combine(dates[0], time.min, tzinfo=doctor_timezone).astimezone(timezone.utc)
        for doctor_timezone, dates in doctor_windows.values()
    ).replace(tzinfo=None)
    window_end_utc = max(
        datetime.combine(dates[-1] + timedelta(days=1), time.min, tzinfo=doctor_timezone)
        .astimezone(timezone.utc)
        for doctor_timezone, dates in doctor_windows.values()
    ).replace(tzinfo=None)

    # Query blackouts in batches or using IN clause
    format_strings = ",".join(["%s"] * len(doctor_ids))
    query_blackouts = f"""
        SELECT doctor_id, start_datetime, end_datetime
        FROM blackout_dates
        WHERE doctor_id IN ({format_strings})
          AND start_datetime < %s
          AND end_datetime > %s
    """
    params = tuple(doctor_ids) + (window_end_utc, window_start_utc)
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

    total_generated = 0
    total_skipped = 0
    generated_day_ranges: dict[tuple[str, date], list[tuple[datetime, datetime]]] = {}

    # 3. Generate slots
    for doc_id, doc_pattern_list in doctor_patterns.items():
        if doc_id not in doctor_windows:
            continue
        doctor_timezone, dates_in_window = doctor_windows[doc_id]
        doc_blackouts = blackouts_by_doctor.get(doc_id, [])

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
                        if any(
                            slot_start_utc < blackout_end
                            and blackout_start < slot_end_utc
                            for blackout_start, blackout_end in doc_blackouts
                        ):
                            slot_start_dt = slot_end_dt
                            continue
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
                            "is_blocked": False,
                            "expires_at": expires_at,
                        }

                        try:
                            slots_table.put_item(
                                Item=item,
                                ConditionExpression=Attr("slot_id").not_exists(),
                            )
                            total_generated += 1
                        except ClientError as exc:
                            if (
                                exc.response.get("Error", {}).get("Code")
                                == "ConditionalCheckFailedException"
                            ):
                                total_skipped += 1
                            else:
                                logger.exception("Unexpected DynamoDB error writing slot %s: %s", slot_id, exc)
                                raise

                        day_ranges.append((slot_start_dt, slot_end_dt))
                        slot_start_dt = slot_end_dt

    logger.info(
        "Slot materialisation completed successfully. total_generated=%d total_skipped=%d",
        total_generated,
        total_skipped,
    )

    return {
        "statusCode": 200,
        "body": {
            "message": "Slot materialisation completed successfully.",
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "total_generated": total_generated,
            "total_skipped": total_skipped,
        },
    }
