import logging
from datetime import timezone
from typing import Any

from shared.database import database_transaction, execute_query, execute_write
from shared.notifications import publish_appointment_notification

logger = logging.getLogger(__name__)
BATCH_SIZE = 200
NO_SHOW_GRACE_MINUTES = 30

WINDOWS = [
    ("REMINDER_24H", "reminder_24h_sent_at",
     "appointment_datetime <= UTC_TIMESTAMP(3) + INTERVAL 24 HOUR "
     "AND appointment_datetime > UTC_TIMESTAMP(3) + INTERVAL 2 HOUR "
     "AND created_at <= appointment_datetime - INTERVAL 24 HOUR"),
    ("REMINDER_2H", "reminder_2h_sent_at",
     "appointment_datetime <= UTC_TIMESTAMP(3) + INTERVAL 2 HOUR "
     "AND appointment_datetime > UTC_TIMESTAMP(3)"),
]


def _send_reminders(event_type: str, column: str, window_sql: str) -> int:
    rows = execute_query(
        f"""SELECT appointment_id, patient_id, consultation_type_id, appointment_datetime
            FROM appointments
            WHERE appointment_status = 'CONFIRMED'
              AND {column} IS NULL
              AND {window_sql}
            ORDER BY appointment_datetime LIMIT %s""",
        (BATCH_SIZE,),
    )
    sent = 0
    for row in rows:
        try:
            start = row["appointment_datetime"]
            if getattr(start, "tzinfo", None) is None:
                start = start.replace(tzinfo=timezone.utc)
            publish_appointment_notification(
                appointment_id=row["appointment_id"],
                event_type=event_type,
                patient_id=row["patient_id"],
                consultation_type_id=row["consultation_type_id"],
                appointment_datetime=start,
                message="Reminder: you have an upcoming appointment.",
                patient_only=True,
            )
            execute_write(
                f"UPDATE appointments SET {column} = UTC_TIMESTAMP(3) "
                f"WHERE appointment_id = %s AND {column} IS NULL",
                (row["appointment_id"],),
            )
            sent += 1
        except Exception:
            logger.exception("Reminder failed for %s.", row["appointment_id"])
    return sent


def _mark_no_shows() -> int:
    rows = execute_query(
        """SELECT appointment_id, patient_id, consultation_type_id, appointment_datetime
           FROM appointments
           WHERE appointment_status = 'CONFIRMED'
             AND appointment_datetime < UTC_TIMESTAMP(3) - INTERVAL %s MINUTE
           ORDER BY appointment_datetime LIMIT %s""",
        (NO_SHOW_GRACE_MINUTES, BATCH_SIZE),
    )
    changed = 0
    for row in rows:
        try:
            with database_transaction() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE appointments SET appointment_status = 'NO_SHOW' WHERE appointment_id = %s AND appointment_status = 'CONFIRMED'",
                        (row["appointment_id"],),
                    )
                    if cursor.rowcount != 1:
                        continue
            start = row["appointment_datetime"]
            if getattr(start, "tzinfo", None) is None:
                start = start.replace(tzinfo=timezone.utc)
            publish_appointment_notification(
                appointment_id=row["appointment_id"],
                event_type="NO_SHOW",
                patient_id=row["patient_id"],
                consultation_type_id=row["consultation_type_id"],
                appointment_datetime=start,
                message="The appointment was marked as a no-show.",
            )
            changed += 1
        except Exception:
            logger.exception("No-show update failed for %s.", row["appointment_id"])
    return changed


def lambda_handler(event: dict[str, Any] | None, context: Any = None) -> dict[str, Any]:
    reminders = sum(_send_reminders(event_type, column, window) for event_type, column, window in WINDOWS)
    no_shows = _mark_no_shows()
    return {"reminders_sent": reminders, "no_shows_marked": no_shows}
