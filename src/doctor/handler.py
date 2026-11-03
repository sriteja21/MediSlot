"""
MediSlot Doctor Business Lambda Handler.

Responsibilities:
- Profile management (view, update, photo upload presigned URL)
- Consultation types (list, create, update, soft-delete)
- Availability patterns (list active, create, delete)
- Blackout dates (list with date range, add with duplicate check, delete)
- Schedule day-view (DynamoDB GSI1-DoctorStart query enriched with RDS appointment counts)
- Doctor dashboard (aggregated profile, upcoming appointments, today's slots, patients served)

Security & Auth:
- Validates authorizer context from API Gateway (doctor_id = authorizer.userId)
- Role verification (doctor)
- Parameterized SQL for all database access
- Secrets are never logged
"""

import base64
import json
import logging
import os
import re
import sys
import uuid
from datetime import date, datetime, time as time_type, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import boto3
from boto3.dynamodb.conditions import Key
from botocore.config import Config
from botocore.exceptions import ClientError

# Ensure shared modules can be imported
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from shared.config import get_required_env, get_ssm_parameter
from shared.database import (
    database_connection,
    database_transaction,
    execute_query,
    execute_write,
)

# ============================================================
# LOGGING SETUP
# ============================================================

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ============================================================
# CONFIGURATION & CONSTANTS
# ============================================================

ENVIRONMENT = os.environ.get("ENVIRONMENT", "dev")
AWS_REGION = os.environ.get("AWS_REGION", "ap-south-1")

SLOTS_TABLE_NAME = (
    os.environ.get("SLOTS_TABLE_NAME")
    or os.environ.get("SLOTS_TABLE")
    or f"MediSlot-Slots-{ENVIRONMENT}"
)
SLOT_MATERIALISER_FUNCTION_NAME = os.environ.get(
    "SLOT_MATERIALISER_FUNCTION_NAME",
    "",
)

PROFILE_PHOTO_BUCKET = (
    os.environ.get("PROFILE_PHOTO_BUCKET")
    or os.environ.get("PROFILE_PHOTOS_BUCKET")
)

ALLOWED_CONSULTATION_KINDS = {"QUICK", "STANDARD", "DETAILED", "HEALTH_CAMP"}

CORS_HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "GET,POST,PUT,PATCH,DELETE,OPTIONS",
}

ROUTE_METHODS = {
    "/doctors/me": {"GET", "PATCH"},
    "/doctors/me/photo-upload-url": {"GET"},
    "/doctors/me/consultation-types": {"GET", "POST"},
    "/doctors/me/consultation-types/{id}": {"PATCH", "DELETE"},
    "/doctors/me/availability": {"GET", "POST"},
    "/doctors/me/availability/{id}": {"PATCH", "DELETE"},
    "/doctors/me/blackouts": {"GET", "POST"},
    "/doctors/me/blackouts/{id}": {"DELETE"},
    "/doctors/me/schedule": {"GET"},
    "/doctors/me/dashboard": {"GET"},
}

# AWS resource singletons
_s3_client = None
_dynamodb_resource = None
_slots_table = None
_lambda_client = None
_doctor_columns_cache = None


def get_s3_client():
    """Return cached boto3 S3 client."""
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3", region_name=AWS_REGION)
    return _s3_client


def get_slots_table():
    """Return cached DynamoDB Table resource for SLOTS."""
    global _dynamodb_resource, _slots_table
    if _slots_table is None:
        _dynamodb_resource = boto3.resource("dynamodb", region_name=AWS_REGION)
        _slots_table = _dynamodb_resource.Table(SLOTS_TABLE_NAME)
    return _slots_table


def trigger_slot_materialisation() -> None:
    global _lambda_client
    if not SLOT_MATERIALISER_FUNCTION_NAME:
        logger.warning("Slot materializer function name is not configured.")
        return

    if _lambda_client is None:
        _lambda_client = boto3.client(
            "lambda",
            region_name=AWS_REGION,
            config=Config(
                connect_timeout=3,
                read_timeout=5,
                retries={"mode": "standard", "total_max_attempts": 1},
            ),
        )

    try:
        _lambda_client.invoke(
            FunctionName=SLOT_MATERIALISER_FUNCTION_NAME,
            InvocationType="Event",
            Payload=b"{}",
        )
    except Exception:
        logger.exception("Could not trigger slot materialization.")


def get_local_day_utc_bounds(day: date, timezone_name: str) -> tuple[datetime, datetime]:
    local_timezone = ZoneInfo(timezone_name or "UTC")
    local_start = datetime.combine(day, time_type.min, tzinfo=local_timezone)
    local_end = datetime.combine(day + timedelta(days=1), time_type.min, tzinfo=local_timezone)
    return (
        local_start.astimezone(timezone.utc),
        local_end.astimezone(timezone.utc),
    )


def query_doctor_slots(
    doctor_id: str,
    utc_start: datetime,
    utc_end: datetime,
) -> list[dict[str, Any]]:
    start_key = utc_start.strftime("%Y-%m-%dT%H:%M:%SZ") + "#"
    end_key = utc_end.strftime("%Y-%m-%dT%H:%M:%SZ")
    table = get_slots_table()
    slots: list[dict[str, Any]] = []
    query_args = {
        "IndexName": "GSI1-DoctorStart",
        "KeyConditionExpression": (
            Key("doctorId").eq(doctor_id)
            & Key("startUtcConsultationType").between(start_key, end_key)
        ),
    }

    while True:
        result = table.query(**query_args)
        slots.extend(result.get("Items", []))
        last_key = result.get("LastEvaluatedKey")
        if not last_key:
            break
        query_args["ExclusiveStartKey"] = last_key

    return slots


# ============================================================
# JSON SERIALIZATION HELPER
# ============================================================

class MediSlotJSONEncoder(json.JSONEncoder):
    """Custom JSON encoder handling Decimals, dates, and timedeltas."""

    def default(self, obj: Any) -> Any:
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        if isinstance(obj, timedelta):
            total_seconds = int(obj.total_seconds())
            hours, rem = divmod(total_seconds, 3600)
            minutes, seconds = divmod(rem, 60)
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        if isinstance(obj, time_type):
            return obj.isoformat()
        if isinstance(obj, set):
            return list(obj)
        return super().default(obj)


def format_response(status_code: int, body: Any) -> dict[str, Any]:
    """Return a standard API Gateway response dictionary."""
    return {
        "statusCode": status_code,
        "headers": CORS_HEADERS,
        "body": json.dumps(body, cls=MediSlotJSONEncoder),
    }


# ============================================================
# REQUEST PARSING HELPERS
# ============================================================

def get_http_method(event: dict[str, Any] | None) -> str:
    """Extract and uppercase the HTTP method from API Gateway event."""
    if not isinstance(event, dict):
        return ""
    return (
        event.get("httpMethod")
        or (event.get("requestContext") or {}).get("http", {}).get("method")
        or ""
    ).upper()


def get_path(event: dict[str, Any] | None) -> str:
    """Extract the raw request path from API Gateway event."""
    if not isinstance(event, dict):
        return "/"
    return (
        event.get("path")
        or event.get("rawPath")
        or (event.get("requestContext") or {}).get("http", {}).get("path")
        or "/"
    )


def normalize_path(path: str) -> str:
    """Strip leading stage names and normalize trailing slashes."""
    if not path:
        return "/"
    path = path.strip()
    if not path.startswith("/"):
        path = "/" + path
    if path != "/":
        path = path.rstrip("/")

    # Strip stage prefix e.g. /dev/doctors/me -> /doctors/me
    parts = path.split("/")
    if len(parts) > 2 and parts[1].lower() in {"dev", "test", "prod", "v1", "v2"}:
        path = "/" + "/".join(parts[2:])

    return path


def parse_route(raw_path: str) -> tuple[str, str | None]:
    """
    Parse a normalized URL path into a recognized route pattern and resource ID.

    Returns:
        tuple (route_pattern, resource_id)
    """
    path = normalize_path(raw_path)
    segments = [s for s in path.split("/") if s]

    if len(segments) >= 2 and segments[0].lower() == "doctors" and segments[1].lower() == "me":
        if len(segments) == 2:
            return ("/doctors/me", None)

        third = segments[2].lower()
        if len(segments) == 3:
            if third == "photo-upload-url":
                return ("/doctors/me/photo-upload-url", None)
            if third == "consultation-types":
                return ("/doctors/me/consultation-types", None)
            if third == "availability":
                return ("/doctors/me/availability", None)
            if third == "blackouts":
                return ("/doctors/me/blackouts", None)
            if third == "schedule":
                return ("/doctors/me/schedule", None)
            if third == "dashboard":
                return ("/doctors/me/dashboard", None)

        elif len(segments) == 4:
            resource_id = segments[3]
            if third == "consultation-types":
                return ("/doctors/me/consultation-types/{id}", resource_id)
            if third == "availability":
                return ("/doctors/me/availability/{id}", resource_id)
            if third == "blackouts":
                return ("/doctors/me/blackouts/{id}", resource_id)

    return (path, None)


def get_request_body(event: dict[str, Any] | None) -> dict[str, Any]:
    """Parse JSON request body from API Gateway event."""
    if not isinstance(event, dict):
        return {}

    body = event.get("body")
    if isinstance(body, dict):
        return body
    if not body:
        return {}

    if event.get("isBase64Encoded"):
        try:
            body = base64.b64decode(body).decode("utf-8")
        except Exception as exc:
            raise ValueError("Failed to decode base64 request body.") from exc

    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
        raise ValueError("Request body must contain valid JSON.") from exc

    if not isinstance(parsed, dict):
        raise ValueError("Request body must be a JSON object.")

    return parsed


def get_doctor_table_columns() -> set[str]:
    """
    Inspect the columns present in the 'doctors' table in RDS.
    Cached for the lifetime of the Lambda execution environment.
    """
    global _doctor_columns_cache
    if _doctor_columns_cache is not None:
        return _doctor_columns_cache

    try:
        rows = execute_query(
            """
            SELECT COLUMN_NAME
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'doctors'
            """
        )
        _doctor_columns_cache = {row["COLUMN_NAME"].lower() for row in rows}
    except Exception as exc:
        logger.warning("Could not query information_schema for doctors columns: %s", exc)
        _doctor_columns_cache = {
            "id", "email", "password_hash", "full_name", "licence_number",
            "specialisation_id", "hospital_id", "years_experience",
            "qualifications", "timezone", "photo_s3_key", "is_active",
            "created_at", "updated_at",
        }

    return _doctor_columns_cache


def get_doctor_specialisations(
    doctor_id: str,
    primary_id: int,
    primary_name: str | None,
) -> list[dict[str, Any]]:
    rows = execute_query(
        """
        SELECT s.id, s.name
        FROM doctor_specialisations ds
        JOIN specialisations s ON s.id = ds.specialisation_id
        WHERE ds.doctor_id = %s
        ORDER BY s.name ASC
        """,
        (doctor_id,),
    )
    if rows:
        return rows
    if primary_name:
        return [{"id": primary_id, "name": primary_name}]
    return []


# ============================================================
# ROUTE HANDLERS
# ============================================================

def get_doctor_profile(doctor_id: str) -> dict[str, Any]:
    """
    GET /doctors/me
    Retrieve the authenticated doctor's full profile including specialisation,
    hospital details, and spoken languages.
    """
    has_phone = "phone" in get_doctor_table_columns()
    phone_select = ", d.phone" if has_phone else ""

    query = f"""
        SELECT
            d.id,
            d.email,
            d.full_name,
            d.licence_number,
            d.specialisation_id,
            s.name AS specialisation_name,
            d.hospital_id,
            h.name AS hospital_name,
            h.address_line AS hospital_address,
            h.city AS hospital_city,
            h.state AS hospital_state,
            h.postal_code AS hospital_postal_code,
            d.years_experience,
            d.qualifications,
            d.timezone,
            d.photo_s3_key,
            d.is_active,
            d.created_at,
            d.updated_at
            {phone_select}
        FROM doctors d
        LEFT JOIN specialisations s ON d.specialisation_id = s.id
        LEFT JOIN hospitals h ON d.hospital_id = h.id
        WHERE d.id = %s
    """
    rows = execute_query(query, (doctor_id,))
    if not rows:
        return format_response(404, {"error": "Doctor not found."})

    doctor = rows[0]
    doctor["specialisations"] = get_doctor_specialisations(
        doctor_id,
        int(doctor["specialisation_id"]),
        doctor.get("specialisation_name"),
    )

    # Retrieve languages
    lang_rows = execute_query(
        "SELECT language FROM doctor_languages WHERE doctor_id = %s ORDER BY language ASC",
        (doctor_id,),
    )
    doctor["languages"] = [r["language"] for r in lang_rows]

    if not has_phone:
        doctor["phone"] = None

    doctor["is_active"] = bool(doctor["is_active"])
    doctor["years_experience"] = int(doctor["years_experience"] or 0)

    if doctor.get("photo_s3_key") and PROFILE_PHOTO_BUCKET:
        doctor["photo_url"] = get_s3_client().generate_presigned_url(
            "get_object",
            Params={
                "Bucket": PROFILE_PHOTO_BUCKET,
                "Key": doctor["photo_s3_key"],
            },
            ExpiresIn=3600,
        )

    return format_response(200, doctor)


def update_doctor_profile(doctor_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """
    PATCH /doctors/me
    Update doctor profile fields: full_name, qualifications, years_experience,
    phone, languages list.
    """
    if not body:
        raise ValueError("Request body cannot be empty.")

    table_columns = get_doctor_table_columns()
    update_fields: list[str] = []
    update_values: list[Any] = []

    if "full_name" in body:
        full_name = body["full_name"]
        if not isinstance(full_name, str) or not full_name.strip():
            raise ValueError("full_name must be a non-empty string.")
        update_fields.append("full_name = %s")
        update_values.append(full_name.strip()[:150])

    if "qualifications" in body:
        qualifications = body["qualifications"]
        if qualifications is not None and not isinstance(qualifications, str):
            raise ValueError("qualifications must be a string or null.")
        update_fields.append("qualifications = %s")
        update_values.append(qualifications.strip()[:500] if qualifications else None)

    if "years_experience" in body:
        years_exp = body["years_experience"]
        if isinstance(years_exp, bool) or not isinstance(years_exp, int) or not 0 <= years_exp <= 255:
            raise ValueError("years_experience must be an integer between 0 and 255.")
        update_fields.append("years_experience = %s")
        update_values.append(years_exp)

    if "phone" in body:
        phone_val = body["phone"]
        if phone_val is not None and not isinstance(phone_val, str):
            raise ValueError("phone must be a string or null.")
        if "phone" in table_columns:
            update_fields.append("phone = %s")
            update_values.append(phone_val.strip()[:20] if phone_val else None)
        else:
            logger.info("phone column not in doctors table schema; skipping column update.")

    languages_to_set = None
    if "languages" in body:
        languages_raw = body["languages"]
        if not isinstance(languages_raw, list):
            raise ValueError("languages must be a list of strings.")
        languages_clean: list[str] = []
        for item in languages_raw:
            if not isinstance(item, str) or not item.strip():
                continue
            lang_str = item.strip()[:50]
            if lang_str not in languages_clean:
                languages_clean.append(lang_str)
        languages_to_set = languages_clean

    if not update_fields and languages_to_set is None:
        raise ValueError("No valid profile fields provided for update.")

    # Execute updates atomically in transaction
    with database_transaction() as conn:
        with conn.cursor() as cursor:
            if update_fields:
                sql = f"UPDATE doctors SET {', '.join(update_fields)} WHERE id = %s"
                cursor.execute(sql, (*update_values, doctor_id))

            if languages_to_set is not None:
                cursor.execute("DELETE FROM doctor_languages WHERE doctor_id = %s", (doctor_id,))
                for lang in languages_to_set:
                    cursor.execute(
                        "INSERT INTO doctor_languages (doctor_id, language) VALUES (%s, %s)",
                        (doctor_id, lang),
                    )

    # Return updated profile
    return get_doctor_profile(doctor_id)


def get_photo_upload_url(
    doctor_id: str,
    content_type: str,
) -> dict[str, Any]:
    """
    GET /doctors/me/photo-upload-url
    Generate a presigned S3 PUT URL for uploading doctor profile photo.
    """
    bucket = PROFILE_PHOTO_BUCKET
    if not bucket:
        raise RuntimeError("PROFILE_PHOTO_BUCKET environment variable is not configured.")

    extensions = {
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
    }
    extension = extensions.get(content_type)
    if not extension:
        raise ValueError("Photo must be JPEG, PNG, or WebP.")

    s3_key = f"doctors/{doctor_id}/profile.{extension}"
    s3 = get_s3_client()

    upload_url = s3.generate_presigned_url(
        "put_object",
        Params={
            "Bucket": bucket,
            "Key": s3_key,
            "ContentType": content_type,
        },
        ExpiresIn=300,
    )

    execute_write(
        "UPDATE doctors SET photo_s3_key = %s WHERE id = %s",
        (s3_key, doctor_id),
    )

    return format_response(
        200,
        {
            "upload_url": upload_url,
            "photo_key": s3_key,
            "expires_in": 300,
        },
    )


def list_consultation_types(doctor_id: str, query_params: dict[str, str]) -> dict[str, Any]:
    """
    GET /doctors/me/consultation-types
    List doctor's consultation types. Active only unless ?all=true is passed.
    """
    include_all = (query_params.get("all") or "").lower() == "true"

    if include_all:
        query = """
            SELECT id, doctor_id, kind, duration_minutes, fee_amount, capacity, is_active
            FROM consultation_types
            WHERE doctor_id = %s
            ORDER BY kind ASC
        """
        rows = execute_query(query, (doctor_id,))
    else:
        query = """
            SELECT id, doctor_id, kind, duration_minutes, fee_amount, capacity, is_active
            FROM consultation_types
            WHERE doctor_id = %s AND is_active = TRUE
            ORDER BY kind ASC
        """
        rows = execute_query(query, (doctor_id,))

    for r in rows:
        r["fee_amount"] = float(r["fee_amount"])
        r["is_active"] = bool(r["is_active"])
        r["capacity"] = int(r["capacity"])
        r["duration_minutes"] = int(r["duration_minutes"])

    return format_response(200, rows)


def create_consultation_type(doctor_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """
    POST /doctors/me/consultation-types
    Create a consultation type using kind, duration, fee, and capacity.
    """
    kind = body.get("kind")
    if not isinstance(kind, str) or kind.upper() not in ALLOWED_CONSULTATION_KINDS:
        raise ValueError(
            f"kind must be one of: {', '.join(sorted(ALLOWED_CONSULTATION_KINDS))}."
        )
    kind = kind.upper()

    duration_minutes = body.get("duration_minutes")
    if (
        isinstance(duration_minutes, bool)
        or not isinstance(duration_minutes, int)
        or duration_minutes <= 0
        or duration_minutes > 1440
    ):
        raise ValueError("duration_minutes must be a positive integer up to 1440.")

    fee_amount = body.get("fee_amount")
    if fee_amount is None:
        raise ValueError("fee_amount is required.")
    try:
        fee_decimal = Decimal(str(fee_amount))
        if fee_decimal < 0:
            raise ValueError()
    except Exception:
        raise ValueError("fee_amount must be a non-negative number.")

    capacity = body.get("capacity")
    if kind == "HEALTH_CAMP":
        if capacity is None:
            capacity = 5
        elif (
            isinstance(capacity, bool)
            or not isinstance(capacity, int)
            or not 2 <= capacity <= 10
        ):
            raise ValueError("capacity for HEALTH_CAMP must be between 2 and 10.")
    else:
        if capacity is not None and capacity != 1:
            raise ValueError(f"capacity for {kind} must be 1.")
        capacity = 1

    # Keep one consultation configuration per kind for each doctor.
    existing = execute_query(
        "SELECT id FROM consultation_types WHERE doctor_id = %s AND kind = %s",
        (doctor_id, kind),
    )
    if existing:
        return format_response(
            409,
            {"error": f"A {kind} consultation type already exists for this doctor."},
        )

    new_id = str(uuid.uuid4())
    execute_write(
        """
        INSERT INTO consultation_types (
            id, doctor_id, kind, duration_minutes, fee_amount, capacity, is_active
        ) VALUES (%s, %s, %s, %s, %s, %s, TRUE)
        """,
        (new_id, doctor_id, kind, duration_minutes, fee_decimal, capacity),
    )
    trigger_slot_materialisation()

    created_record = {
        "id": new_id,
        "doctor_id": doctor_id,
        "kind": kind,
        "duration_minutes": duration_minutes,
        "fee_amount": float(fee_decimal),
        "capacity": capacity,
        "is_active": True,
    }
    return format_response(201, created_record)


def update_consultation_type(
    doctor_id: str,
    ct_id: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    """
    PATCH /doctors/me/consultation-types/{id}
    Update consultation type fee, duration, capacity, or active status.
    """
    rows = execute_query(
        "SELECT * FROM consultation_types WHERE id = %s AND doctor_id = %s",
        (ct_id, doctor_id),
    )
    if not rows:
        return format_response(404, {"error": "Consultation type not found."})

    existing = rows[0]
    kind = existing["kind"]
    update_clauses: list[str] = []
    params: list[Any] = []

    if "fee_amount" in body:
        try:
            fee_decimal = Decimal(str(body["fee_amount"]))
            if fee_decimal < 0:
                raise ValueError()
        except Exception:
            raise ValueError("fee_amount must be a non-negative number.")
        update_clauses.append("fee_amount = %s")
        params.append(fee_decimal)

    if "duration_minutes" in body:
        dur = body["duration_minutes"]
        if isinstance(dur, bool) or not isinstance(dur, int) or dur <= 0 or dur > 1440:
            raise ValueError("duration_minutes must be a positive integer up to 1440.")
        update_clauses.append("duration_minutes = %s")
        params.append(dur)

    if "capacity" in body:
        cap = body["capacity"]
        if kind == "HEALTH_CAMP":
            if isinstance(cap, bool) or not isinstance(cap, int) or not 2 <= cap <= 10:
                raise ValueError("capacity for HEALTH_CAMP must be between 2 and 10.")
        else:
            if cap != 1:
                raise ValueError(f"capacity for {kind} must be 1.")
        update_clauses.append("capacity = %s")
        params.append(cap)

    if "is_active" in body:
        is_act = body["is_active"]
        if not isinstance(is_act, bool):
            raise ValueError("is_active must be a boolean.")
        update_clauses.append("is_active = %s")
        params.append(is_act)

    if not update_clauses:
        raise ValueError("No valid fields provided for update.")

    sql = f"UPDATE consultation_types SET {', '.join(update_clauses)} WHERE id = %s AND doctor_id = %s"
    params.extend([ct_id, doctor_id])
    execute_write(sql, tuple(params))
    trigger_slot_materialisation()

    # Fetch updated record
    updated_rows = execute_query(
        "SELECT * FROM consultation_types WHERE id = %s AND doctor_id = %s",
        (ct_id, doctor_id),
    )
    record = updated_rows[0]
    record["fee_amount"] = float(record["fee_amount"])
    record["is_active"] = bool(record["is_active"])
    record["capacity"] = int(record["capacity"])
    record["duration_minutes"] = int(record["duration_minutes"])

    return format_response(200, record)


def delete_consultation_type(doctor_id: str, ct_id: str) -> dict[str, Any]:
    """
    DELETE /doctors/me/consultation-types/{id}
    Soft-delete consultation type by setting is_active=false.
    """
    rows = execute_query(
        "SELECT id, is_active FROM consultation_types WHERE id = %s AND doctor_id = %s",
        (ct_id, doctor_id),
    )
    if not rows:
        return format_response(404, {"error": "Consultation type not found."})

    execute_write(
        "UPDATE consultation_types SET is_active = FALSE WHERE id = %s AND doctor_id = %s",
        (ct_id, doctor_id),
    )
    trigger_slot_materialisation()
    return format_response(
        200,
        {
            "message": "Consultation type deactivated successfully.",
            "id": ct_id,
            "is_active": False,
        },
    )


def list_availability(doctor_id: str) -> dict[str, Any]:
    """
    GET /doctors/me/availability
    List active recurring availability patterns for the doctor.
    """
    query = """
        SELECT
            ap.id,
            ap.doctor_id,
            ap.day_of_week,
            ap.start_local_time,
            ap.end_local_time,
            ap.consultation_type_id,
            ct.kind AS consultation_type_name,
            ct.duration_minutes,
            ct.fee_amount,
            ap.is_active,
            ap.updated_at
        FROM availability_patterns ap
        LEFT JOIN consultation_types ct ON ap.consultation_type_id = ct.id
        WHERE ap.doctor_id = %s AND ap.is_active = TRUE
        ORDER BY ap.day_of_week ASC, ap.start_local_time ASC
    """
    rows = execute_query(query, (doctor_id,))
    for r in rows:
        r["is_active"] = bool(r["is_active"])
        if r["fee_amount"] is not None:
            r["fee_amount"] = float(r["fee_amount"])
        # Format timedeltas from MySQL TIME column to HH:MM:SS
        for time_key in ("start_local_time", "end_local_time"):
            val = r[time_key]
            if isinstance(val, timedelta):
                secs = int(val.total_seconds())
                h, rem = divmod(secs, 3600)
                m, s = divmod(rem, 60)
                r[time_key] = f"{h:02d}:{m:02d}:{s:02d}"

    return format_response(200, rows)


def _validate_time_str(time_str: Any, field_name: str) -> str:
    """Validate and normalize HH:MM or HH:MM:SS string."""
    if not isinstance(time_str, str) or not time_str.strip():
        raise ValueError(f"{field_name} is required in HH:MM format.")
    time_str = time_str.strip()

    pattern = r"^([01]\d|2[0-3]):[0-5]\d(:[0-5]\d)?$"
    if not re.fullmatch(pattern, time_str):
        raise ValueError(f"{field_name} must be a valid 24-hour time string (HH:MM or HH:MM:SS).")

    parts = time_str.split(":")
    h = int(parts[0])
    m = int(parts[1])
    s = int(parts[2]) if len(parts) == 3 else 0
    return f"{h:02d}:{m:02d}:{s:02d}"


def create_availability(doctor_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """
    POST /doctors/me/availability
    Create a recurring availability pattern: day_of_week (1-7), start_local_time,
    end_local_time, consultation_type_id.
    """
    day_of_week = body.get("day_of_week")
    if (
        isinstance(day_of_week, bool)
        or not isinstance(day_of_week, int)
        or not 1 <= day_of_week <= 7
    ):
        raise ValueError("day_of_week must be an integer between 1 and 7 (1=Monday, 7=Sunday).")

    start_local_time = _validate_time_str(body.get("start_local_time"), "start_local_time")
    end_local_time = _validate_time_str(body.get("end_local_time"), "end_local_time")

    if end_local_time <= start_local_time:
        raise ValueError("end_local_time must be strictly after start_local_time.")

    ct_id = body.get("consultation_type_id")
    if not isinstance(ct_id, str) or not ct_id.strip():
        raise ValueError("consultation_type_id is required.")
    ct_id = ct_id.strip()

    # Validate consultation type belongs to doctor and is active
    ct_rows = execute_query(
        "SELECT id FROM consultation_types WHERE id = %s AND doctor_id = %s AND is_active = TRUE",
        (ct_id, doctor_id),
    )
    if not ct_rows:
        return format_response(
            400,
            {"error": "Invalid consultation_type_id or consultation type is not active."},
        )

    pattern_id = str(uuid.uuid4())
    execute_write(
        """
        INSERT INTO availability_patterns (
            id, doctor_id, day_of_week, start_local_time, end_local_time, consultation_type_id, is_active
        ) VALUES (%s, %s, %s, %s, %s, %s, TRUE)
        """,
        (pattern_id, doctor_id, day_of_week, start_local_time, end_local_time, ct_id),
    )
    trigger_slot_materialisation()

    created_pattern = {
        "id": pattern_id,
        "doctor_id": doctor_id,
        "day_of_week": day_of_week,
        "start_local_time": start_local_time,
        "end_local_time": end_local_time,
        "consultation_type_id": ct_id,
        "is_active": True,
    }
    return format_response(201, created_pattern)


def delete_availability(doctor_id: str, pattern_id: str) -> dict[str, Any]:
    """
    DELETE /doctors/me/availability/{id}
    Delete an availability pattern.
    """
    rows = execute_query(
        "SELECT id FROM availability_patterns WHERE id = %s AND doctor_id = %s",
        (pattern_id, doctor_id),
    )
    if not rows:
        return format_response(404, {"error": "Availability pattern not found."})

    execute_write(
        "DELETE FROM availability_patterns WHERE id = %s AND doctor_id = %s",
        (pattern_id, doctor_id),
    )
    trigger_slot_materialisation()
    return format_response(
        200,
        {
            "message": "Availability pattern deleted successfully.",
            "id": pattern_id,
        },
    )


def update_availability(
    doctor_id: str,
    pattern_id: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    if not body:
        raise ValueError("Request body cannot be empty.")

    existing_rows = execute_query(
        "SELECT id FROM availability_patterns WHERE id = %s AND doctor_id = %s",
        (pattern_id, doctor_id),
    )
    if not existing_rows:
        return format_response(404, {"error": "Availability pattern not found."})

    day_of_week = body.get("day_of_week")
    if (
        isinstance(day_of_week, bool)
        or not isinstance(day_of_week, int)
        or not 1 <= day_of_week <= 7
    ):
        raise ValueError("day_of_week must be an integer from 1 to 7.")

    start_local_time = _validate_time_str(body.get("start_local_time"), "start_local_time")
    end_local_time = _validate_time_str(body.get("end_local_time"), "end_local_time")
    if end_local_time <= start_local_time:
        raise ValueError("end_local_time must be after start_local_time.")

    consultation_type_id = body.get("consultation_type_id")
    if not isinstance(consultation_type_id, str) or not consultation_type_id.strip():
        raise ValueError("consultation_type_id is required.")
    consultation_type_id = consultation_type_id.strip()

    consultation_rows = execute_query(
        """
        SELECT id FROM consultation_types
        WHERE id = %s AND doctor_id = %s AND is_active = TRUE
        """,
        (consultation_type_id, doctor_id),
    )
    if not consultation_rows:
        raise ValueError("consultation_type_id must identify an active type belonging to this doctor.")

    execute_write(
        """
        UPDATE availability_patterns
        SET day_of_week = %s,
            start_local_time = %s,
            end_local_time = %s,
            consultation_type_id = %s
        WHERE id = %s AND doctor_id = %s
        """,
        (
            day_of_week,
            start_local_time,
            end_local_time,
            consultation_type_id,
            pattern_id,
            doctor_id,
        ),
    )
    trigger_slot_materialisation()
    return format_response(
        200,
        {
            "id": pattern_id,
            "doctor_id": doctor_id,
            "day_of_week": day_of_week,
            "start_local_time": start_local_time,
            "end_local_time": end_local_time,
            "consultation_type_id": consultation_type_id,
        },
    )


def list_blackouts(doctor_id: str, query_params: dict[str, str]) -> dict[str, Any]:
    """
    GET /doctors/me/blackouts
    List blackout dates. Optional ?from=YYYY-MM-DD&to=YYYY-MM-DD filtering.
    """
    from_date = query_params.get("from")
    to_date = query_params.get("to")

    query = "SELECT id, doctor_id, blackout_date, reason FROM blackout_dates WHERE doctor_id = %s"
    params: list[Any] = [doctor_id]

    if from_date:
        try:
            parsed_from = datetime.strptime(from_date.strip(), "%Y-%m-%d").date()
            query += " AND blackout_date >= %s"
            params.append(parsed_from.isoformat())
        except ValueError:
            raise ValueError("Query parameter 'from' must be in YYYY-MM-DD format.")

    if to_date:
        try:
            parsed_to = datetime.strptime(to_date.strip(), "%Y-%m-%d").date()
            query += " AND blackout_date <= %s"
            params.append(parsed_to.isoformat())
        except ValueError:
            raise ValueError("Query parameter 'to' must be in YYYY-MM-DD format.")

    query += " ORDER BY blackout_date ASC"
    rows = execute_query(query, tuple(params))

    for r in rows:
        if isinstance(r["blackout_date"], (date, datetime)):
            r["blackout_date"] = r["blackout_date"].isoformat()

    return format_response(200, rows)


def create_blackout(doctor_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """
    POST /doctors/me/blackouts
    Add a blackout date (blackout_date YYYY-MM-DD, optional reason).
    Checks for duplicate date using SELECT before INSERT, returns 409 if duplicate.
    """
    blackout_date_raw = body.get("blackout_date")
    if not isinstance(blackout_date_raw, str) or not blackout_date_raw.strip():
        raise ValueError("blackout_date is required in YYYY-MM-DD format.")

    try:
        b_date = datetime.strptime(blackout_date_raw.strip(), "%Y-%m-%d").date()
    except ValueError:
        raise ValueError("blackout_date must be a valid date in YYYY-MM-DD format.")

    reason = body.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise ValueError("reason must be a string or null.")
    clean_reason = reason.strip()[:200] if reason else None

    # Check for duplicate blackout date using SELECT before INSERT
    existing = execute_query(
        "SELECT id FROM blackout_dates WHERE doctor_id = %s AND blackout_date = %s",
        (doctor_id, b_date.isoformat()),
    )
    if existing:
        return format_response(
            409,
            {"error": f"Blackout date '{b_date.isoformat()}' already exists for this doctor."},
        )

    blackout_id = str(uuid.uuid4())
    execute_write(
        "INSERT INTO blackout_dates (id, doctor_id, blackout_date, reason) VALUES (%s, %s, %s, %s)",
        (blackout_id, doctor_id, b_date.isoformat(), clean_reason),
    )
    trigger_slot_materialisation()

    created_blackout = {
        "id": blackout_id,
        "doctor_id": doctor_id,
        "blackout_date": b_date.isoformat(),
        "reason": clean_reason,
    }
    return format_response(201, created_blackout)


def delete_blackout(doctor_id: str, blackout_id: str) -> dict[str, Any]:
    """
    DELETE /doctors/me/blackouts/{id}
    Delete a blackout date.
    """
    rows = execute_query(
        "SELECT id FROM blackout_dates WHERE id = %s AND doctor_id = %s",
        (blackout_id, doctor_id),
    )
    if not rows:
        return format_response(404, {"error": "Blackout date not found."})

    execute_write(
        "DELETE FROM blackout_dates WHERE id = %s AND doctor_id = %s",
        (blackout_id, doctor_id),
    )
    trigger_slot_materialisation()
    return format_response(
        200,
        {
            "message": "Blackout date deleted successfully.",
            "id": blackout_id,
        },
    )


def get_schedule(doctor_id: str, query_params: dict[str, str]) -> dict[str, Any]:
    """
    GET /doctors/me/schedule?date=YYYY-MM-DD
    Day-view: query GSI1-DoctorStart over the doctor's local day and enrich
    the slots with appointment counts from RDS.
    """
    date_str = query_params.get("date")
    if not date_str or not date_str.strip():
        raise ValueError("Query parameter 'date' (YYYY-MM-DD) is required.")

    try:
        parsed_date = datetime.strptime(date_str.strip(), "%Y-%m-%d").date()
        clean_date_str = parsed_date.isoformat()
    except ValueError:
        raise ValueError("Query parameter 'date' must be in YYYY-MM-DD format.")

    doctor_rows = execute_query(
        "SELECT timezone FROM doctors WHERE id = %s",
        (doctor_id,),
    )
    if not doctor_rows:
        return format_response(404, {"error": "Doctor not found."})

    timezone_name = str(doctor_rows[0].get("timezone") or "UTC")
    try:
        utc_start, utc_end = get_local_day_utc_bounds(parsed_date, timezone_name)
    except Exception as exc:
        raise ValueError("Doctor timezone is invalid.") from exc

    slots = query_doctor_slots(doctor_id, utc_start, utc_end)
    utc_start_db = utc_start.replace(tzinfo=None)
    utc_end_db = utc_end.replace(tzinfo=None)

    # Enrich with appointment counts from RDS appointments table
    rds_counts_query = """
        SELECT
            slot_id,
            COUNT(*) AS total_count,
            SUM(CASE WHEN status = 'CONFIRMED' THEN 1 ELSE 0 END) AS confirmed_count,
            SUM(CASE WHEN status = 'COMPLETED' THEN 1 ELSE 0 END) AS completed_count,
            SUM(CASE WHEN status IN ('CONFIRMED', 'COMPLETED') THEN 1 ELSE 0 END) AS active_count
        FROM appointments
        WHERE doctor_id = %s
          AND slot_start_utc >= %s
          AND slot_start_utc < %s
        GROUP BY slot_id
    """
    count_rows = execute_query(
        rds_counts_query,
        (doctor_id, utc_start_db, utc_end_db),
    )

    counts_by_slot: dict[str, dict[str, int]] = {}
    for cr in count_rows:
        slot_key = cr["slot_id"]
        counts_by_slot[slot_key] = {
            "total": int(cr["total_count"] or 0),
            "confirmed": int(cr["confirmed_count"] or 0),
            "completed": int(cr["completed_count"] or 0),
            "active": int(cr["active_count"] or 0),
        }

    for slot in slots:
        s_id = slot.get("slotId") or slot.get("slot_id")
        slot_counts = counts_by_slot.get(
            s_id,
            {"total": 0, "confirmed": 0, "completed": 0, "active": 0},
        )
        slot["appointment_count"] = slot_counts["active"]
        slot["confirmed_count"] = slot_counts["confirmed"]
        slot["total_appointments"] = slot_counts["total"]
        if "booked_count" not in slot:
            slot["booked_count"] = slot_counts["active"]

    # Retrieve appointments on this date from RDS
    appointments_query = """
        SELECT
                        a.id,
                        a.patient_id,
                        p.full_name AS patient_name,
                        a.doctor_id,
                        a.consultation_type_id,
                        a.slot_id,
                        a.slot_start_utc,
                        a.slot_end_utc,
                        a.status,
                        a.fee_amount,
                        a.booked_at
                FROM appointments a
                LEFT JOIN patients p ON p.id = a.patient_id
                WHERE a.doctor_id = %s
                    AND a.slot_start_utc >= %s
                    AND a.slot_start_utc < %s
                ORDER BY a.slot_start_utc ASC
    """
    appointments = execute_query(
        appointments_query,
        (doctor_id, utc_start_db, utc_end_db),
    )
    for appt in appointments:
        appt["fee_amount"] = float(appt["fee_amount"])

    # Sort slots chronologically
    slots.sort(key=lambda s: str(s.get("start_time", "")))

    return format_response(
        200,
        {
            "date": clean_date_str,
            "slots": slots,
            "appointments": appointments,
            "total_slots": len(slots),
            "total_appointments": len(appointments),
        },
    )


def get_dashboard(doctor_id: str) -> dict[str, Any]:
    """
    GET /doctors/me/dashboard
    Aggregate dashboard: profile summary, upcoming appointments count,
    today's slots count, and total patients served.
    """
    # 1. Profile summary
    has_phone = "phone" in get_doctor_table_columns()
    phone_select = ", d.phone" if has_phone else ""

    profile_query = f"""
        SELECT
            d.id,
            d.email,
            d.full_name,
            d.licence_number,
            d.specialisation_id,
            s.name AS specialisation_name,
            d.hospital_id,
            h.name AS hospital_name,
            h.city AS hospital_city,
            d.years_experience,
            d.qualifications,
            d.timezone,
            d.photo_s3_key,
            d.is_active
            {phone_select}
        FROM doctors d
        LEFT JOIN specialisations s ON d.specialisation_id = s.id
        LEFT JOIN hospitals h ON d.hospital_id = h.id
        WHERE d.id = %s
    """
    profile_rows = execute_query(profile_query, (doctor_id,))
    if not profile_rows:
        return format_response(404, {"error": "Doctor not found."})

    profile = profile_rows[0]
    profile["specialisations"] = get_doctor_specialisations(
        doctor_id,
        int(profile["specialisation_id"]),
        profile.get("specialisation_name"),
    )
    profile["is_active"] = bool(profile["is_active"])
    profile["years_experience"] = int(profile["years_experience"] or 0)
    if not has_phone:
        profile["phone"] = None

    lang_rows = execute_query(
        "SELECT language FROM doctor_languages WHERE doctor_id = %s ORDER BY language ASC",
        (doctor_id,),
    )
    profile["languages"] = [r["language"] for r in lang_rows]

    # 2. Upcoming appointments count (CONFIRMED in future)
    upcoming_query = """
        SELECT COUNT(*) AS cnt
        FROM appointments
        WHERE doctor_id = %s
          AND status = 'CONFIRMED'
          AND slot_start_utc >= UTC_TIMESTAMP()
    """
    upcoming_rows = execute_query(upcoming_query, (doctor_id,))
    upcoming_appointments_count = int(upcoming_rows[0]["cnt"]) if upcoming_rows else 0

    # 3. Today's slots count from DynamoDB GSI1-DoctorStart
    today_slots_count = 0
    timezone_name = str(profile.get("timezone") or "UTC")
    local_today = datetime.now(ZoneInfo(timezone_name)).date()
    utc_start, utc_end = get_local_day_utc_bounds(local_today, timezone_name)

    today_appointments_rows = execute_query(
        """
        SELECT COUNT(*) AS cnt
        FROM appointments
        WHERE doctor_id = %s
          AND status = 'CONFIRMED'
          AND slot_start_utc >= %s
          AND slot_start_utc < %s
        """,
        (doctor_id, utc_start.replace(tzinfo=None), utc_end.replace(tzinfo=None)),
    )
    today_appointments_count = (
        int(today_appointments_rows[0]["cnt"])
        if today_appointments_rows
        else 0
    )

    try:
        today_slots_count = len(query_doctor_slots(doctor_id, utc_start, utc_end))
    except Exception as exc:
        logger.warning("Could not query DynamoDB for dashboard today's slots: %s", exc)
        rds_slots = execute_query(
            """
            SELECT COUNT(DISTINCT slot_id) AS cnt
            FROM appointments
            WHERE doctor_id = %s
              AND slot_start_utc >= %s
              AND slot_start_utc < %s
            """,
            (doctor_id, utc_start.replace(tzinfo=None), utc_end.replace(tzinfo=None)),
        )
        today_slots_count = int(rds_slots[0]["cnt"]) if rds_slots else 0

    # 4. Total patients served (distinct patients with COMPLETED appointments)
    patients_query = """
        SELECT COUNT(DISTINCT patient_id) AS cnt
        FROM appointments
        WHERE doctor_id = %s
          AND status = 'COMPLETED'
    """
    patients_rows = execute_query(patients_query, (doctor_id,))
    total_patients_served = int(patients_rows[0]["cnt"]) if patients_rows else 0

    active_types_rows = execute_query(
        """
        SELECT COUNT(*) AS cnt
        FROM consultation_types
        WHERE doctor_id = %s AND is_active = TRUE
        """,
        (doctor_id,),
    )
    active_consultation_types = int(active_types_rows[0]["cnt"]) if active_types_rows else 0

    recent_appointments = execute_query(
        """
        SELECT
            a.id,
            a.patient_id,
            p.full_name AS patient_name,
            a.slot_start_utc,
            a.status,
            a.fee_amount,
            ct.kind AS consultation_type_name
        FROM appointments a
        LEFT JOIN patients p ON p.id = a.patient_id
        LEFT JOIN consultation_types ct ON ct.id = a.consultation_type_id
        WHERE a.doctor_id = %s
        ORDER BY a.booked_at DESC
        LIMIT 8
        """,
        (doctor_id,),
    )
    for appointment in recent_appointments:
        appointment["fee_amount"] = float(appointment["fee_amount"] or 0)

    dashboard_data = {
        "profile_summary": profile,
        "profile": profile,
        "doctor_name": profile["full_name"],
        "today_appointments": today_appointments_count,
        "active_consultation_types": active_consultation_types,
        "recent_appointments": recent_appointments,
        "upcoming_appointments_count": upcoming_appointments_count,
        "upcoming_appointments": upcoming_appointments_count,
        "today_slots_count": today_slots_count,
        "today_slots": today_slots_count,
        "total_patients_served": total_patients_served,
    }
    return format_response(200, dashboard_data)


# ============================================================
# MAIN ENTRY POINT
# ============================================================

def lambda_handler(event: dict[str, Any] | None, context: Any = None) -> dict[str, Any]:
    """
    MediSlot Doctor Business Lambda entry point.
    Dispatches API Gateway requests based on HTTP method and path.
    """
    if not isinstance(event, dict):
        event = {}

    method = get_http_method(event)
    raw_path = get_path(event)

    logger.info("Doctor Lambda invoked: method=%s, path=%s", method, raw_path)

    try:
        # Handle CORS OPTIONS preflight directly
        if method == "OPTIONS":
            return format_response(200, {"status": "ok"})

        # Authorizer verification
        authorizer = (event.get("requestContext") or {}).get("authorizer") or {}
        doctor_id = authorizer.get("userId")
        role = authorizer.get("role")

        if not doctor_id:
            logger.warning("Unauthorized request: missing authorizer userId.")
            return format_response(401, {"error": "Unauthorized: Missing authentication context."})

        if role and role.lower() != "doctor":
            logger.warning("Forbidden request: role '%s' is not doctor.", role)
            return format_response(403, {"error": "Forbidden: Doctor role required."})

        # Route matching
        route_pattern, resource_id = parse_route(raw_path)

        if route_pattern not in ROUTE_METHODS:
            logger.warning("Route not found: %s (raw: %s)", route_pattern, raw_path)
            return format_response(404, {"error": f"Route not found: {raw_path}"})

        if method not in ROUTE_METHODS[route_pattern]:
            logger.warning("Method not allowed: %s for %s", method, route_pattern)
            return format_response(
                405,
                {"error": f"Method {method} not allowed for {raw_path}"},
            )

        body = get_request_body(event)
        query_params = event.get("queryStringParameters") or {}

        # Route dispatch
        if route_pattern == "/doctors/me":
            if method == "GET":
                return get_doctor_profile(doctor_id)
            elif method == "PATCH":
                return update_doctor_profile(doctor_id, body)

        elif route_pattern == "/doctors/me/photo-upload-url":
            return get_photo_upload_url(
                doctor_id,
                str(query_params.get("content_type") or ""),
            )

        elif route_pattern == "/doctors/me/consultation-types":
            if method == "GET":
                return list_consultation_types(doctor_id, query_params)
            elif method == "POST":
                return create_consultation_type(doctor_id, body)

        elif route_pattern == "/doctors/me/consultation-types/{id}":
            if not resource_id:
                return format_response(400, {"error": "Consultation type ID is required in URL path."})
            if method == "PATCH":
                return update_consultation_type(doctor_id, resource_id, body)
            elif method == "DELETE":
                return delete_consultation_type(doctor_id, resource_id)

        elif route_pattern == "/doctors/me/availability":
            if method == "GET":
                return list_availability(doctor_id)
            elif method == "POST":
                return create_availability(doctor_id, body)

        elif route_pattern == "/doctors/me/availability/{id}":
            if not resource_id:
                return format_response(400, {"error": "Availability pattern ID is required in URL path."})
            if method == "PATCH":
                return update_availability(doctor_id, resource_id, body)
            return delete_availability(doctor_id, resource_id)

        elif route_pattern == "/doctors/me/blackouts":
            if method == "GET":
                return list_blackouts(doctor_id, query_params)
            elif method == "POST":
                return create_blackout(doctor_id, body)

        elif route_pattern == "/doctors/me/blackouts/{id}":
            if not resource_id:
                return format_response(400, {"error": "Blackout date ID is required in URL path."})
            return delete_blackout(doctor_id, resource_id)

        elif route_pattern == "/doctors/me/schedule":
            return get_schedule(doctor_id, query_params)

        elif route_pattern == "/doctors/me/dashboard":
            return get_dashboard(doctor_id)

        return format_response(404, {"error": f"Route not found: {raw_path}"})

    except ValueError as exc:
        logger.info("Validation error: %s", exc)
        return format_response(400, {"error": str(exc)})

    except Exception as exc:
        logger.exception("Internal error handling request: %s", exc)
        return format_response(500, {"error": "Internal server error."})
