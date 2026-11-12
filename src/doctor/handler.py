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
import os
import re
import sys
import time
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
from shared.logging_utils import (
    current_correlation_id,
    extract_field_names,
    get_correlation_id,
    get_logger,
    log_error,
    log_exception,
    log_info,
    log_warning,
    reset_correlation_id,
    set_correlation_id,
)

# ============================================================
# LOGGING SETUP
# ============================================================

logger = get_logger("doctor")

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
_DOCTOR_VALIDATION_FIELDS = {
    "blackout_date",
    "capacity",
    "consultation_type_id",
    "content_type",
    "date",
    "day_of_week",
    "end_local_time",
    "experience_years",
    "fee_amount",
    "full_name",
    "is_active",
    "languages",
    "phone",
    "qualifications",
    "reason",
    "start_local_time",
    "years_experience",
}

CORS_HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization,X-Correlation-ID",
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
            Payload=json.dumps(
                {
                    "headers": {
                        "X-Correlation-ID": current_correlation_id() or "",
                    }
                }
            ).encode("utf-8"),
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
    start_key = utc_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_key = (utc_end - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    table = get_slots_table()
    slots: list[dict[str, Any]] = []
    query_args = {
        "IndexName": "gsi_doctor_time",
        "KeyConditionExpression": (
            Key("doctor_id").eq(doctor_id)
            & Key("start_time").between(start_key, end_key)
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
    headers = dict(CORS_HEADERS)
    headers["X-Correlation-ID"] = current_correlation_id() or ""
    return {
        "statusCode": status_code,
        "headers": headers,
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
            "doctor_id", "user_id", "first_name", "last_name", "license_number",
            "hospital_id", "experience_years", "qualification", "timezone",
            "profile_image_key", "phone_number", "languages", "is_verified",
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
        SELECT s.specialization_id AS id, s.name
        FROM doctor_specializations ds
        JOIN specializations s ON s.specialization_id = ds.specialization_id
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
    query = """
        SELECT
            d.doctor_id AS id,
            u.email,
            CONCAT_WS(' ', d.first_name, d.last_name) AS full_name,
            d.license_number AS licence_number,
            d.experience_years AS years_experience,
            d.qualification AS qualifications,
            d.phone_number AS phone,
            d.languages,
            d.is_verified,
            d.timezone,
            d.profile_image_key AS photo_s3_key,
            d.hospital_id,
            h.name AS hospital_name,
            h.address AS hospital_address,
            u.is_active,
            d.created_at,
            d.updated_at
        FROM doctors d
        JOIN users u ON u.user_id = d.user_id
        LEFT JOIN hospitals h ON d.hospital_id = h.hospital_id
        WHERE d.doctor_id = %s
    """
    rows = execute_query(query, (doctor_id,))
    if not rows:
        return format_response(404, {"error": "Doctor not found."})

    doctor = rows[0]
    doctor["specialisations"] = get_doctor_specialisations(doctor_id, 0, None)
    doctor["languages"] = [
        value.strip()
        for value in (doctor.get("languages") or "").split(",")
        if value.strip()
    ]

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

    update_fields: list[str] = []
    update_values: list[Any] = []

    if "full_name" in body:
        full_name = body["full_name"]
        if not isinstance(full_name, str) or not full_name.strip():
            raise ValueError("full_name must be a non-empty string.")
        name_parts = full_name.strip().split(maxsplit=1)
        update_fields.extend(["first_name = %s", "last_name = %s"])
        update_values.extend([name_parts[0][:100], name_parts[1][:100] if len(name_parts) > 1 else ""])

    if "qualifications" in body:
        qualifications = body["qualifications"]
        if qualifications is not None and not isinstance(qualifications, str):
            raise ValueError("qualifications must be a string or null.")
        update_fields.append("qualification = %s")
        update_values.append(qualifications.strip()[:500] if qualifications else None)

    if "years_experience" in body:
        years_exp = body["years_experience"]
        if isinstance(years_exp, bool) or not isinstance(years_exp, int) or not 0 <= years_exp <= 255:
            raise ValueError("years_experience must be an integer between 0 and 255.")
        update_fields.append("experience_years = %s")
        update_values.append(years_exp)

    if "phone" in body:
        phone_val = body["phone"]
        if phone_val is not None and not isinstance(phone_val, str):
            raise ValueError("phone must be a string or null.")
        update_fields.append("phone_number = %s")
        update_values.append(phone_val.strip()[:20] if phone_val else None)

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
                sql = f"UPDATE doctors SET {', '.join(update_fields)} WHERE doctor_id = %s"
                cursor.execute(sql, (*update_values, doctor_id))

            if languages_to_set is not None:
                cursor.execute(
                    "UPDATE doctors SET languages = %s WHERE doctor_id = %s",
                    (", ".join(languages_to_set) or None, doctor_id),
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
        "UPDATE doctors SET profile_image_key = %s WHERE doctor_id = %s",
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

    query = """
        SELECT ct.consultation_type_id AS id, ct.doctor_id,
               catalog.consultation_type_id AS catalog_type_id,
               catalog.name AS kind, catalog.duration_minutes,
               ct.fee AS fee_amount, ct.capacity, ct.is_active
        FROM consultation_types ct
        JOIN consultation_catalog catalog
          ON catalog.consultation_type_id = ct.catalog_type_id
        WHERE ct.doctor_id = %s
    """
    params: list[Any] = [doctor_id]
    if not include_all:
        query += " AND ct.is_active = TRUE"
    query += " ORDER BY catalog.name ASC"
    rows = execute_query(query, tuple(params))

    for r in rows:
        r["fee_amount"] = float(r["fee_amount"])
        r["is_active"] = bool(r["is_active"])
        r["capacity"] = int(r["capacity"])
        r["duration_minutes"] = int(r["duration_minutes"])

    return format_response(200, rows)


def create_consultation_type(doctor_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """
    POST /doctors/me/consultation-types
    Add a fixed catalog consultation type to a doctor, with doctor-specific fee/capacity.
    """
    kind = body.get("kind")
    if not isinstance(kind, str) or kind.upper() not in ALLOWED_CONSULTATION_KINDS:
        raise ValueError(
            f"kind must be one of: {', '.join(sorted(ALLOWED_CONSULTATION_KINDS))}."
        )
    kind = kind.upper()

    catalog_rows = execute_query(
        """
        SELECT consultation_type_id, name, duration_minutes
        FROM consultation_catalog
        WHERE consultation_type_id = %s
        """,
        (kind,),
    )
    if not catalog_rows:
        raise ValueError("Selected consultation type is not in the fixed catalog.")
    catalog_type = catalog_rows[0]

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
        "SELECT consultation_type_id FROM consultation_types WHERE doctor_id = %s AND catalog_type_id = %s",
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
            consultation_type_id, doctor_id, catalog_type_id, fee, capacity, is_active
        ) VALUES (%s, %s, %s, %s, %s, TRUE)
        """,
        (new_id, doctor_id, kind, fee_decimal, capacity),
    )
    trigger_slot_materialisation()

    created_record = {
        "id": new_id,
        "doctor_id": doctor_id,
        "kind": catalog_type["name"],
        "catalog_type_id": kind,
        "duration_minutes": int(catalog_type["duration_minutes"]),
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
    Update doctor-specific fee, capacity, or active status. Duration is fixed in the catalog.
    """
    rows = execute_query(
        """
         SELECT ct.consultation_type_id, ct.doctor_id,
             catalog.name AS kind, catalog.duration_minutes,
             ct.fee AS fee_amount, ct.capacity, ct.is_active
         FROM consultation_types ct
         JOIN consultation_catalog catalog
           ON catalog.consultation_type_id = ct.catalog_type_id
         WHERE ct.consultation_type_id = %s AND ct.doctor_id = %s
        """,
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
        update_clauses.append("fee = %s")
        params.append(fee_decimal)

    if "duration_minutes" in body:
        raise ValueError("Consultation duration is fixed by the catalog and cannot be edited.")

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

    sql = f"UPDATE consultation_types SET {', '.join(update_clauses)} WHERE consultation_type_id = %s AND doctor_id = %s"
    params.extend([ct_id, doctor_id])
    execute_write(sql, tuple(params))
    trigger_slot_materialisation()

    # Fetch updated record
    updated_rows = execute_query(
        """
         SELECT ct.consultation_type_id AS id, ct.doctor_id,
             catalog.name AS kind, catalog.consultation_type_id AS catalog_type_id,
             catalog.mode, catalog.duration_minutes,
             ct.fee AS fee_amount, ct.capacity, ct.is_active
         FROM consultation_types ct
         JOIN consultation_catalog catalog
           ON catalog.consultation_type_id = ct.catalog_type_id
         WHERE ct.consultation_type_id = %s AND ct.doctor_id = %s
        """,
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
        "SELECT consultation_type_id AS id, is_active FROM consultation_types WHERE consultation_type_id = %s AND doctor_id = %s",
        (ct_id, doctor_id),
    )
    if not rows:
        return format_response(404, {"error": "Consultation type not found."})

    execute_write(
        "UPDATE consultation_types SET is_active = FALSE WHERE consultation_type_id = %s AND doctor_id = %s",
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
            ap.pattern_id AS id,
            ct.doctor_id,
            ap.day_of_week,
            ap.start_time AS start_local_time,
            ap.end_time AS end_local_time,
            ap.consultation_type_id,
            catalog.name AS consultation_type_name,
            catalog.duration_minutes,
            ct.fee AS fee_amount,
            ct.capacity,
            ap.is_active,
            ap.updated_at
        FROM availability_patterns ap
        JOIN consultation_types ct ON ap.consultation_type_id = ct.consultation_type_id
        JOIN consultation_catalog catalog ON catalog.consultation_type_id = ct.catalog_type_id
        WHERE ct.doctor_id = %s AND ap.is_active = TRUE
        ORDER BY ap.day_of_week ASC, ap.start_time ASC
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


def times_overlap(start_a: str, end_a: str, start_b: str, end_b: str) -> bool:
    """Return true when two HH:MM[:SS] windows overlap on the same day."""
    def to_minutes(value: str) -> int:
        parts = value.split(":")
        hours = int(parts[0])
        minutes = int(parts[1])
        seconds = int(parts[2]) if len(parts) > 2 else 0
        return hours * 60 * 60 + minutes * 60 + seconds

    return to_minutes(start_a) < to_minutes(end_b) and to_minutes(start_b) < to_minutes(end_a)


def find_overlapping_availability_pattern(
    doctor_id: str,
    day_of_week: int,
    start_local_time: str,
    end_local_time: str,
    ignore_pattern_id: str | None = None,
) -> dict[str, Any] | None:
    """Return an existing pattern that overlaps with the supplied window."""
    query = """
                SELECT ap.pattern_id AS id, ap.start_time AS start_local_time,
                             ap.end_time AS end_local_time
                FROM availability_patterns ap
                JOIN consultation_types ct
                    ON ct.consultation_type_id = ap.consultation_type_id
                WHERE ct.doctor_id = %s AND ap.day_of_week = %s AND ap.is_active = TRUE
    """
    params: list[Any] = [doctor_id, day_of_week]

    if ignore_pattern_id:
        query += " AND ap.pattern_id != %s"
        params.append(ignore_pattern_id)

    query += " ORDER BY ap.start_time ASC"
    rows = execute_query(query, tuple(params))

    for row in rows:
        if times_overlap(
            start_local_time,
            end_local_time,
            row["start_local_time"],
            row["end_local_time"],
        ):
            return row
    return None


def create_availability(doctor_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """
    POST /doctors/me/availability
    Create a recurring availability pattern: day_of_week (0-6, Sunday=0), start_local_time,
    end_local_time, consultation_type_id.
    """
    day_of_week = body.get("day_of_week")
    if (
        isinstance(day_of_week, bool)
        or not isinstance(day_of_week, int)
        or not 0 <= day_of_week <= 6
    ):
        raise ValueError("day_of_week must be an integer between 0 and 6 (0=Sunday).")

    start_local_time = _validate_time_str(body.get("start_local_time"), "start_local_time")
    end_local_time = _validate_time_str(body.get("end_local_time"), "end_local_time")

    if end_local_time <= start_local_time:
        raise ValueError("end_local_time must be strictly after start_local_time.")

    existing_overlap = find_overlapping_availability_pattern(
        doctor_id,
        day_of_week,
        start_local_time,
        end_local_time,
    )
    if existing_overlap:
        raise ValueError(
            "Availability overlaps with an existing pattern for this doctor: "
            f"{start_local_time}-{end_local_time} conflicts with "
            f"{existing_overlap['start_local_time']}-{existing_overlap['end_local_time']}."
        )

    ct_id = body.get("consultation_type_id")
    if not isinstance(ct_id, str) or not ct_id.strip():
        raise ValueError("consultation_type_id is required.")
    ct_id = ct_id.strip()

    # Validate consultation type belongs to doctor and is active
    ct_rows = execute_query(
        "SELECT consultation_type_id FROM consultation_types WHERE consultation_type_id = %s AND doctor_id = %s AND is_active = TRUE",
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
            pattern_id, consultation_type_id, day_of_week, start_time, end_time, is_active
        ) VALUES (%s, %s, %s, %s, %s, TRUE)
        """,
        (pattern_id, ct_id, day_of_week, start_local_time, end_local_time),
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
        """
        SELECT ap.pattern_id
        FROM availability_patterns ap
        JOIN consultation_types ct ON ct.consultation_type_id = ap.consultation_type_id
        WHERE ap.pattern_id = %s AND ct.doctor_id = %s
        """,
        (pattern_id, doctor_id),
    )
    if not rows:
        return format_response(404, {"error": "Availability pattern not found."})

    execute_write(
        "DELETE FROM availability_patterns WHERE pattern_id = %s",
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
        """
        SELECT ap.pattern_id
        FROM availability_patterns ap
        JOIN consultation_types ct ON ct.consultation_type_id = ap.consultation_type_id
        WHERE ap.pattern_id = %s AND ct.doctor_id = %s
        """,
        (pattern_id, doctor_id),
    )
    if not existing_rows:
        return format_response(404, {"error": "Availability pattern not found."})

    day_of_week = body.get("day_of_week")
    if (
        isinstance(day_of_week, bool)
        or not isinstance(day_of_week, int)
        or not 0 <= day_of_week <= 6
    ):
        raise ValueError("day_of_week must be an integer from 0 to 6 (0=Sunday).")

    start_local_time = _validate_time_str(body.get("start_local_time"), "start_local_time")
    end_local_time = _validate_time_str(body.get("end_local_time"), "end_local_time")
    if end_local_time <= start_local_time:
        raise ValueError("end_local_time must be after start_local_time.")

    existing_overlap = find_overlapping_availability_pattern(
        doctor_id,
        day_of_week,
        start_local_time,
        end_local_time,
        pattern_id,
    )
    if existing_overlap:
        raise ValueError(
            "Availability overlaps with an existing pattern for this doctor: "
            f"{start_local_time}-{end_local_time} conflicts with "
            f"{existing_overlap['start_local_time']}-{existing_overlap['end_local_time']}."
        )

    consultation_type_id = body.get("consultation_type_id")
    if not isinstance(consultation_type_id, str) or not consultation_type_id.strip():
        raise ValueError("consultation_type_id is required.")
    consultation_type_id = consultation_type_id.strip()

    consultation_rows = execute_query(
        """
        SELECT consultation_type_id FROM consultation_types
        WHERE consultation_type_id = %s AND doctor_id = %s AND is_active = TRUE
        """,
        (consultation_type_id, doctor_id),
    )
    if not consultation_rows:
        raise ValueError("consultation_type_id must identify an active type belonging to this doctor.")

    execute_write(
        """
        UPDATE availability_patterns
        SET day_of_week = %s,
            start_time = %s,
            end_time = %s,
            consultation_type_id = %s
        WHERE pattern_id = %s
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

    parsed_from = None
    parsed_to = None
    try:
        if from_date:
            parsed_from = datetime.strptime(from_date.strip(), "%Y-%m-%d").date()
        if to_date:
            parsed_to = datetime.strptime(to_date.strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("Blackout date filters must use YYYY-MM-DD format.") from exc

    doctor_rows = execute_query(
        "SELECT timezone FROM doctors WHERE doctor_id = %s", (doctor_id,)
    )
    timezone_name = str(doctor_rows[0].get("timezone") or "UTC") if doctor_rows else "UTC"
    doctor_timezone = ZoneInfo(timezone_name)
    rows = execute_query(
        """
        SELECT blackout_id AS id, doctor_id, start_datetime, end_datetime, reason
        FROM blackout_dates
        WHERE doctor_id = %s
        ORDER BY start_datetime ASC
        """,
        (doctor_id,),
    )

    filtered_rows = []
    for row in rows:
        start_value = row["start_datetime"]
        end_value = row["end_datetime"]
        start_utc = start_value.replace(tzinfo=timezone.utc) if start_value.tzinfo is None else start_value
        end_utc = end_value.replace(tzinfo=timezone.utc) if end_value.tzinfo is None else end_value
        local_start = start_utc.astimezone(doctor_timezone)
        local_end = end_utc.astimezone(doctor_timezone)
        blackout_date = local_start.date()
        if parsed_from and blackout_date < parsed_from:
            continue
        if parsed_to and blackout_date > parsed_to:
            continue
        row["blackout_date"] = blackout_date.isoformat()
        row["start_datetime"] = start_utc.isoformat()
        row["end_datetime"] = end_utc.isoformat()
        filtered_rows.append(row)

    return format_response(200, filtered_rows)


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

    doctor_rows = execute_query(
        "SELECT timezone FROM doctors WHERE doctor_id = %s", (doctor_id,)
    )
    if not doctor_rows:
        return format_response(404, {"error": "Doctor not found."})
    doctor_timezone = ZoneInfo(str(doctor_rows[0].get("timezone") or "UTC"))
    local_start = datetime.combine(b_date, time_type.min, tzinfo=doctor_timezone)
    local_end = datetime.combine(
        b_date + timedelta(days=1), time_type.min, tzinfo=doctor_timezone
    )
    start_utc = local_start.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = local_end.astimezone(timezone.utc).replace(tzinfo=None)

    # MySQL has no exclusion constraint, so reject intersecting ranges here.
    existing = execute_query(
        """
        SELECT blackout_id AS id, start_datetime, end_datetime
        FROM blackout_dates
        WHERE doctor_id = %s AND start_datetime < %s AND end_datetime > %s
        LIMIT 1
        """,
        (doctor_id, end_utc, start_utc),
    )
    if existing:
        return format_response(
            409,
            {"error": f"Blackout date '{b_date.isoformat()}' overlaps an existing blackout."},
        )

    blackout_id = str(uuid.uuid4())
    execute_write(
        """
        INSERT INTO blackout_dates (blackout_id, doctor_id, start_datetime, end_datetime, reason)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (blackout_id, doctor_id, start_utc, end_utc, clean_reason),
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
        "SELECT blackout_id AS id FROM blackout_dates WHERE blackout_id = %s AND doctor_id = %s",
        (blackout_id, doctor_id),
    )
    if not rows:
        return format_response(404, {"error": "Blackout date not found."})

    execute_write(
        "DELETE FROM blackout_dates WHERE blackout_id = %s AND doctor_id = %s",
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
        "SELECT timezone FROM doctors WHERE doctor_id = %s",
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
            a.consultation_type_id,
            a.appointment_datetime,
            COUNT(*) AS total_count,
            SUM(CASE WHEN a.appointment_status = 'CONFIRMED' THEN 1 ELSE 0 END) AS confirmed_count,
            SUM(CASE WHEN a.appointment_status = 'COMPLETED' THEN 1 ELSE 0 END) AS completed_count,
            SUM(CASE WHEN a.appointment_status IN ('CONFIRMED', 'COMPLETED') THEN 1 ELSE 0 END) AS active_count
        FROM appointments a
        JOIN consultation_types ct ON ct.consultation_type_id = a.consultation_type_id
        WHERE ct.doctor_id = %s
          AND a.appointment_datetime >= %s
          AND a.appointment_datetime < %s
        GROUP BY a.consultation_type_id, a.appointment_datetime
    """
    count_rows = execute_query(
        rds_counts_query,
        (doctor_id, utc_start_db, utc_end_db),
    )

    counts_by_slot: dict[str, dict[str, int]] = {}
    for cr in count_rows:
        appointment_datetime = cr["appointment_datetime"]
        if appointment_datetime.tzinfo is None:
            appointment_datetime = appointment_datetime.replace(tzinfo=timezone.utc)
        slot_key = (
            f"{cr['consultation_type_id']}#{int(appointment_datetime.timestamp())}"
        )
        counts_by_slot[slot_key] = {
            "total": int(cr["total_count"] or 0),
            "confirmed": int(cr["confirmed_count"] or 0),
            "completed": int(cr["completed_count"] or 0),
            "active": int(cr["active_count"] or 0),
        }

    for slot in slots:
        s_id = slot.get("slot_id")
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
                        a.appointment_id AS id,
                        a.patient_id,
                        CONCAT_WS(' ', p.first_name, p.last_name) AS patient_name,
                        ct.doctor_id,
                        a.consultation_type_id,
                        a.appointment_datetime AS slot_start_utc,
                        a.appointment_status AS status,
                        COALESCE(
                                (SELECT pay.amount FROM payments pay
                                 WHERE pay.appointment_id = a.appointment_id
                                     AND pay.payment_status = 'SUCCESS'
                                 ORDER BY pay.created_at DESC LIMIT 1),
                                ct.fee
                        ) AS fee_amount,
                        catalog.duration_minutes,
                        a.created_at AS booked_at
                FROM appointments a
                JOIN consultation_types ct ON ct.consultation_type_id = a.consultation_type_id
                    JOIN consultation_catalog catalog ON catalog.consultation_type_id = ct.catalog_type_id
                LEFT JOIN patients p ON p.patient_id = a.patient_id
                WHERE ct.doctor_id = %s
                    AND a.appointment_datetime >= %s
                    AND a.appointment_datetime < %s
                ORDER BY a.appointment_datetime ASC
    """
    appointments = execute_query(
        appointments_query,
        (doctor_id, utc_start_db, utc_end_db),
    )
    for appt in appointments:
        appt["fee_amount"] = float(appt["fee_amount"])
        appointment_datetime = appt["slot_start_utc"]
        if appointment_datetime.tzinfo is None:
            appointment_datetime = appointment_datetime.replace(tzinfo=timezone.utc)
        appt["slot_start_utc"] = appointment_datetime.isoformat()
        appt["slot_id"] = (
            f"{appt['consultation_type_id']}#{int(appointment_datetime.timestamp())}"
        )
        appt["slot_end_utc"] = (
            appointment_datetime + timedelta(minutes=int(appt.pop("duration_minutes")))
        ).isoformat()

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
    profile_query = """
        SELECT
            d.doctor_id AS id,
            u.email,
            CONCAT_WS(' ', d.first_name, d.last_name) AS full_name,
            d.license_number AS licence_number,
            d.experience_years AS years_experience,
            d.qualification AS qualifications,
            d.phone_number AS phone,
            d.languages,
            d.is_verified,
            d.profile_image_key AS photo_s3_key,
            d.hospital_id,
            h.name AS hospital_name,
            h.address AS hospital_address,
            d.timezone,
            u.is_active
        FROM doctors d
        JOIN users u ON u.user_id = d.user_id
        LEFT JOIN hospitals h ON d.hospital_id = h.hospital_id
        WHERE d.doctor_id = %s
    """
    profile_rows = execute_query(profile_query, (doctor_id,))
    if not profile_rows:
        return format_response(404, {"error": "Doctor not found."})

    profile = profile_rows[0]
    profile["specialisations"] = get_doctor_specialisations(doctor_id, 0, None)
    profile["is_active"] = bool(profile["is_active"])
    profile["years_experience"] = int(profile["years_experience"] or 0)
    profile["languages"] = [
        value.strip()
        for value in (profile.get("languages") or "").split(",")
        if value.strip()
    ]

    # 2. Upcoming appointments count (CONFIRMED in future)
    upcoming_query = """
        SELECT COUNT(*) AS cnt
                FROM appointments a
                JOIN consultation_types ct ON ct.consultation_type_id = a.consultation_type_id
                WHERE ct.doctor_id = %s
                    AND a.appointment_status = 'CONFIRMED'
                    AND a.appointment_datetime >= UTC_TIMESTAMP()
    """
    upcoming_rows = execute_query(upcoming_query, (doctor_id,))
    upcoming_appointments_count = int(upcoming_rows[0]["cnt"]) if upcoming_rows else 0

    # 3. Today's slots count from DynamoDB gsi_doctor_time
    today_slots_count = 0
    timezone_name = str(profile.get("timezone") or "UTC")
    local_today = datetime.now(ZoneInfo(timezone_name)).date()
    utc_start, utc_end = get_local_day_utc_bounds(local_today, timezone_name)

    today_appointments_rows = execute_query(
        """
        SELECT COUNT(*) AS cnt
                FROM appointments a
                JOIN consultation_types ct ON ct.consultation_type_id = a.consultation_type_id
                WHERE ct.doctor_id = %s
                    AND a.appointment_status = 'CONFIRMED'
                    AND a.appointment_datetime >= %s
                    AND a.appointment_datetime < %s
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
                        SELECT COUNT(DISTINCT CONCAT(a.consultation_type_id, '#',
                                             UNIX_TIMESTAMP(a.appointment_datetime))) AS cnt
                        FROM appointments a
                        JOIN consultation_types ct ON ct.consultation_type_id = a.consultation_type_id
                        WHERE ct.doctor_id = %s
                            AND a.appointment_datetime >= %s
                            AND a.appointment_datetime < %s
            """,
            (doctor_id, utc_start.replace(tzinfo=None), utc_end.replace(tzinfo=None)),
        )
        today_slots_count = int(rds_slots[0]["cnt"]) if rds_slots else 0

    # 4. Total patients served (distinct patients with COMPLETED appointments)
    patients_query = """
                SELECT COUNT(DISTINCT a.patient_id) AS cnt
                FROM appointments a
                JOIN consultation_types ct ON ct.consultation_type_id = a.consultation_type_id
                WHERE ct.doctor_id = %s
                    AND a.appointment_status = 'COMPLETED'
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
            a.appointment_id AS id,
            a.patient_id,
            CONCAT_WS(' ', p.first_name, p.last_name) AS patient_name,
            a.appointment_datetime AS slot_start_utc,
            a.appointment_status AS status,
            COALESCE(
                (SELECT pay.amount FROM payments pay
                 WHERE pay.appointment_id = a.appointment_id
                   AND pay.payment_status = 'SUCCESS'
                 ORDER BY pay.created_at DESC LIMIT 1),
                ct.fee
            ) AS fee_amount,
            catalog.name AS consultation_type_name
        FROM appointments a
        LEFT JOIN patients p ON p.patient_id = a.patient_id
        JOIN consultation_types ct ON ct.consultation_type_id = a.consultation_type_id
        JOIN consultation_catalog catalog ON catalog.consultation_type_id = ct.catalog_type_id
        WHERE ct.doctor_id = %s
        ORDER BY a.created_at DESC
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

def _handle_doctor_request(event: dict[str, Any] | None, context: Any = None) -> dict[str, Any]:
    """
    MediSlot Doctor Business Lambda entry point.
    Dispatches API Gateway requests based on HTTP method and path.
    """
    if not isinstance(event, dict):
        event = {}

    method = get_http_method(event)
    raw_path = get_path(event)

    try:
        # Handle CORS OPTIONS preflight directly
        if method == "OPTIONS":
            return format_response(200, {"status": "ok"})

        # Authorizer verification
        authorizer = (event.get("requestContext") or {}).get("authorizer") or {}
        doctor_id = authorizer.get("userId")
        role = authorizer.get("role")

        if not doctor_id:
            return format_response(401, {"error": "Unauthorized: Missing authentication context."})

        if role and role.lower() != "doctor":
            return format_response(403, {"error": "Forbidden: Doctor role required."})

        # Route matching
        route_pattern, resource_id = parse_route(raw_path)

        if route_pattern not in ROUTE_METHODS:
            return format_response(404, {"error": f"Route not found: {raw_path}"})

        if method not in ROUTE_METHODS[route_pattern]:
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
        log_warning(
            "doctor_validation_failed",
            "Doctor API request validation failed",
            current_correlation_id(),
            http_status=400,
            field_names=extract_field_names(
                str(exc), _DOCTOR_VALIDATION_FIELDS
            ),
        )
        return format_response(400, {"error": str(exc)})


def lambda_handler(event: dict[str, Any] | None, context: Any = None) -> dict[str, Any]:
    """Log the request lifecycle and enforce a generic failure response."""
    event = event if isinstance(event, dict) else {}
    correlation_id = get_correlation_id(event, context)
    token = set_correlation_id(correlation_id)
    started = time.perf_counter()
    try:
        method = get_http_method(event)
    except (AttributeError, TypeError):
        method = "UNKNOWN"
    request_context = event.get("requestContext")
    if not isinstance(request_context, dict):
        request_context = {}
    http_context = request_context.get("http")
    if not isinstance(http_context, dict):
        http_context = {}
    route = (
        event.get("resource")
        or event.get("routeKey")
        or http_context.get("routeKey")
        or "/doctors/me"
    )
    authorizer = request_context.get("authorizer")
    if not isinstance(authorizer, dict):
        authorizer = {}
    doctor_id = authorizer.get("userId")
    log_info(
        "doctor_request_started",
        "Doctor API request started",
        correlation_id,
        method=method,
        route=route,
        user_id=doctor_id,
        role="doctor" if doctor_id else None,
    )
    try:
        result = _handle_doctor_request(event, context)
    except Exception as exc:
        log_exception(
            "doctor_request_failed",
            "Doctor API request failed unexpectedly",
            correlation_id,
            error_type=type(exc).__name__,
            error_message="Internal request failure",
            user_id=doctor_id,
            role="doctor" if doctor_id else None,
        )
        result = format_response(500, {"error": "Internal server error."})

    status = int(result.get("statusCode", 500))
    duration_ms = int((time.perf_counter() - started) * 1000)
    if status >= 500:
        log_error(
            "doctor_request_failed",
            "Doctor API request failed unexpectedly",
            correlation_id,
            http_status=status,
            duration_ms=duration_ms,
            error_message="Internal request failure",
            user_id=doctor_id,
            role="doctor" if doctor_id else None,
        )
    if status in {401, 403, 404, 405, 409}:
        log_warning(
            "doctor_request_rejected",
            "Doctor API request was rejected",
            correlation_id,
            http_status=status,
            duration_ms=duration_ms,
            user_id=doctor_id,
            role="doctor" if doctor_id else None,
        )
    log_info(
        "doctor_request_finished",
        "Doctor API request finished",
        correlation_id,
        http_status=status,
        duration_ms=duration_ms,
        user_id=doctor_id,
        role="doctor" if doctor_id else None,
    )
    reset_correlation_id(token)
    return result
