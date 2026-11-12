"""Patient profile and public doctor discovery API."""

import json
import os
import re
import time
from datetime import date, datetime, time as time_type, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import boto3
from boto3.dynamodb.conditions import Key

from shared.database import database_transaction, execute_query
from shared.logging_utils import get_correlation_id, log_error, log_exception, log_info, log_warning


AWS_REGION = os.environ.get("AWS_REGION", "ap-south-1")
ENVIRONMENT = os.environ.get("ENVIRONMENT", "dev")
SLOTS_TABLE_NAME = os.environ.get("SLOTS_TABLE", f"MediSlot-Slots-{ENVIRONMENT}")
REFRESH_TOKENS_TABLE_NAME = os.environ.get(
    "REFRESH_TOKENS_TABLE", f"MediSlot-RefreshTokens-{ENVIRONMENT}"
)
PROFILE_PHOTOS_BUCKET = os.environ.get("PROFILE_PHOTOS_BUCKET", "").strip()
MAX_PAGE_SIZE = int(os.environ.get("MAX_PAGE_SIZE", "0"))
MAX_SLOT_RANGE_DAYS = int(os.environ.get("MAX_SLOT_RANGE_DAYS", "0"))
SLOT_WINDOW_DAYS = int(os.environ.get("SLOT_WINDOW_DAYS", "0"))
PROFILE_PHOTO_URL_TTL_SECONDS = int(os.environ.get("PROFILE_PHOTO_URL_TTL_SECONDS", "900"))
PATIENT_NOTES_MAX_LENGTH = 2000
ALLOWED_GENDERS = {"MALE", "FEMALE", "OTHER"}
PHONE_PATTERN = re.compile(r"\+?[0-9][0-9\s\-]{6,19}\Z")

_dynamodb = None
_slots_table = None
_refresh_tokens_table = None
_s3 = None


def get_slots_table():
    global _dynamodb, _slots_table
    if _slots_table is None:
        _dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
        _slots_table = _dynamodb.Table(SLOTS_TABLE_NAME)
    return _slots_table


def get_refresh_tokens_table():
    global _dynamodb, _refresh_tokens_table
    if _refresh_tokens_table is None:
        if _dynamodb is None:
            _dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
        _refresh_tokens_table = _dynamodb.Table(REFRESH_TOKENS_TABLE_NAME)
    return _refresh_tokens_table


def get_s3_client():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3", region_name=AWS_REGION)
    return _s3


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    if isinstance(value, datetime):
        return value.isoformat() + ("Z" if value.tzinfo is None else "")
    if isinstance(value, date):
        return value.isoformat()
    return value


def _response(status_code: int, payload: dict[str, Any], correlation_id: str) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization,X-Correlation-ID",
            "Access-Control-Allow-Methods": "GET,PUT,DELETE,OPTIONS",
            "X-Correlation-ID": correlation_id,
        },
        "body": json.dumps(payload, default=_json_value),
    }


def _path_and_method(event: dict[str, Any]) -> tuple[str, str, list[str]]:
    request_context = event.get("requestContext") or {}
    method = (
        event.get("httpMethod")
        or (request_context.get("http") or {}).get("method")
        or ""
    ).upper()
    path = (
        event.get("path")
        or event.get("rawPath")
        or (request_context.get("http") or {}).get("path")
        or "/"
    ).split("?", 1)[0].rstrip("/").lower()
    parts = [part for part in path.split("/") if part]
    if parts and parts[0] in {"dev", "test", "prod", "v1", "v2"}:
        parts = parts[1:]
    return method, "/" + "/".join(parts), parts


def _query_params(event: dict[str, Any]) -> dict[str, str]:
    params = event.get("queryStringParameters") or {}
    return {str(key): str(value) for key, value in params.items() if value is not None}


def _patient_user_id(event: dict[str, Any]) -> str | None:
    """The only identity source is the Lambda Authorizer context."""
    authorizer = ((event.get("requestContext") or {}).get("authorizer") or {})
    role = str(authorizer.get("role", "")).lower()
    if role != "patient":
        return None
    user_id = authorizer.get("userId") or authorizer.get("sub")
    return str(user_id).strip() if user_id else None


def _require_patient(event: dict[str, Any]) -> tuple[str | None, int | None]:
    authorizer = ((event.get("requestContext") or {}).get("authorizer") or {})
    role = str(authorizer.get("role", "")).lower()
    if not role:
        return None, 401
    if role != "patient":
        return None, 403
    user_id = _patient_user_id(event)
    return (user_id, None) if user_id else (None, 401)


def _is_public_route(method: str, parts: list[str]) -> bool:
    return method == "GET" and (
        parts == ["doctors"]
        or parts == ["doctors", "options"]
        or (len(parts) == 2 and parts[0] == "doctors" and parts[1] != "me")
    )


def _profile_rows(user_id: str) -> list[dict[str, Any]]:
    return execute_query(
        """
        SELECT p.first_name, p.last_name, p.date_of_birth, p.gender,
               p.phone_number, p.city, p.emergency_contact_name,
               p.emergency_contact_phone, p.notes, u.email
        FROM patients p
        JOIN users u ON u.user_id = p.user_id
        WHERE p.user_id = %s AND u.role = 'PATIENT' AND u.is_active = TRUE
        LIMIT 1
        """,
        (user_id,),
    )


def _public_patient_profile(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": " ".join(part for part in (row.get("first_name"), row.get("last_name")) if part),
        "date_of_birth": row.get("date_of_birth"),
        "gender": str(row["gender"]).lower() if row.get("gender") else None,
        "phone": row.get("phone_number"),
        "email": row.get("email"),
        "city": row.get("city") or "",
        "emergency_contact_name": row.get("emergency_contact_name"),
        "emergency_contact_phone": row.get("emergency_contact_phone"),
        "notes": row.get("notes") or "",
    }


def _validate_profile(payload: Any) -> tuple[dict[str, Any] | None, dict[str, str]]:
    if not isinstance(payload, dict):
        return None, {"profile": "A JSON object is required."}

    allowed = {
        "name", "date_of_birth", "gender", "phone", "city",
        "emergency_contact_name", "emergency_contact_phone", "notes",
    }
    errors = {key: "This field cannot be changed here." for key in payload if key not in allowed}

    name = payload.get("name")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 150:
        errors["name"] = "Enter a name between 1 and 150 characters."

    raw_dob = payload.get("date_of_birth")
    dob = None
    if not isinstance(raw_dob, str):
        errors["date_of_birth"] = "Date of birth is required in YYYY-MM-DD format."
    else:
        try:
            dob = date.fromisoformat(raw_dob)
            if dob >= datetime.now(timezone.utc).date() or dob.year < 1900:
                errors["date_of_birth"] = "Enter a valid past date of birth."
        except ValueError:
            errors["date_of_birth"] = "Enter a valid date in YYYY-MM-DD format."

    gender = payload.get("gender")
    normalized_gender = str(gender).strip().upper() if gender not in (None, "") else None
    if normalized_gender and normalized_gender not in ALLOWED_GENDERS:
        errors["gender"] = "Choose female, male, or other."

    normalized: dict[str, Any] = {}
    for field in ("phone", "emergency_contact_phone"):
        value = payload.get(field)
        value = value.strip() if isinstance(value, str) else value
        if value not in (None, "") and (not isinstance(value, str) or not PHONE_PATTERN.fullmatch(value)):
            errors[field] = "Enter a valid phone number."
        normalized[field] = value or None

    emergency_name = payload.get("emergency_contact_name")
    if emergency_name is not None and not isinstance(emergency_name, str):
        errors["emergency_contact_name"] = "Enter a valid contact name."
    elif isinstance(emergency_name, str) and len(emergency_name.strip()) > 150:
        errors["emergency_contact_name"] = "Contact name must be 150 characters or fewer."
    else:
        normalized["emergency_contact_name"] = emergency_name.strip() or None if isinstance(emergency_name, str) else None

    city = payload.get("city", "")
    if city is not None and (not isinstance(city, str) or len(city.strip()) > 100):
        errors["city"] = "City must be 100 characters or fewer."
    normalized["city"] = city.strip() if isinstance(city, str) else ""

    notes = payload.get("notes", "")
    if notes is not None and (not isinstance(notes, str) or len(notes) > PATIENT_NOTES_MAX_LENGTH):
        errors["notes"] = f"Notes must be {PATIENT_NOTES_MAX_LENGTH} characters or fewer."
    normalized["notes"] = notes if isinstance(notes, str) else ""

    if bool(normalized.get("emergency_contact_name")) != bool(normalized.get("emergency_contact_phone")):
        errors["emergency_contact_name"] = "Enter both emergency contact name and phone, or leave both empty."
        errors["emergency_contact_phone"] = "Enter both emergency contact name and phone, or leave both empty."

    if errors:
        return None, errors

    parts = name.strip().split(maxsplit=1)
    normalized.update(
        {
            "first_name": parts[0],
            "last_name": parts[1] if len(parts) > 1 else "",
            "date_of_birth": dob,
            "gender": normalized_gender,
        }
    )
    return normalized, {}


def _delete_user_refresh_tokens(user_id: str) -> int:
    table = get_refresh_tokens_table()
    deleted = 0
    query_args: dict[str, Any] = {
        "IndexName": "gsi_user",
        "KeyConditionExpression": Key("user_id").eq(user_id),
        "ProjectionExpression": "session_id",
    }
    while True:
        result = table.query(**query_args)
        with table.batch_writer() as batch:
            for item in result.get("Items", []):
                batch.delete_item(Key={"session_id": item["session_id"]})
                deleted += 1
        last_key = result.get("LastEvaluatedKey")
        if not last_key:
            break
        query_args["ExclusiveStartKey"] = last_key
    return deleted


def _validate_search(params: dict[str, str]) -> tuple[dict[str, Any] | None, dict[str, str]]:
    errors: dict[str, str] = {}
    page_raw = params.get("page", "1")
    page_size_raw = params.get("page_size", "10")
    try:
        page = int(page_raw)
        if page < 1:
            raise ValueError
    except ValueError:
        page = 1
        errors["page"] = "Page must be a positive integer."
    try:
        page_size = int(page_size_raw)
        if page_size < 1 or page_size > MAX_PAGE_SIZE:
            raise ValueError
    except ValueError:
        page_size = 1
        errors["page_size"] = f"Page size must be from 1 to {MAX_PAGE_SIZE}."
    sort = params.get("sort", "relevance").strip().lower() or "relevance"
    if sort not in {"relevance", "name", "lowest_fee", "experience"}:
        errors["sort"] = "Sort must be relevance, name, lowest_fee, or experience."
    if errors:
        return None, errors
    filters = {name: params[name].strip() for name in ("name", "hospital", "specialisation", "city") if params.get(name, "").strip()}
    return {"page": page, "page_size": page_size, "sort": sort, "filters": filters}, {}


def _doctor_search(params: dict[str, str]) -> tuple[int, dict[str, Any]]:
    criteria, errors = _validate_search(params)
    if errors:
        return 400, {"message": "Check the highlighted search fields.", "field_errors": errors}

    filters = criteria["filters"]
    where = ["d.is_verified = TRUE", "ct.is_active = TRUE"]
    values: list[Any] = []
    if "name" in filters:
        where.append("CONCAT_WS(' ', d.first_name, d.last_name) LIKE %s")
        values.append(f"%{filters['name']}%")
    if "hospital" in filters:
        where.append("h.name LIKE %s")
        values.append(f"%{filters['hospital']}%")
    if "specialisation" in filters:
        where.append(
            "EXISTS (SELECT 1 FROM doctor_specializations dsf "
            "JOIN specializations sf ON sf.specialization_id = dsf.specialization_id "
            "WHERE dsf.doctor_id = d.doctor_id AND sf.name = %s)"
        )
        values.append(filters["specialisation"])
    if "city" in filters:
        where.append("h.city = %s")
        values.append(filters["city"])

    join_sql = """
        FROM doctors d
        JOIN hospitals h ON h.hospital_id = d.hospital_id
        JOIN consultation_types ct ON ct.doctor_id = d.doctor_id
        LEFT JOIN doctor_specializations ds ON ds.doctor_id = d.doctor_id
        LEFT JOIN specializations s ON s.specialization_id = ds.specialization_id
    """
    where_sql = " WHERE " + " AND ".join(where)
    count_rows = execute_query(
        "SELECT COUNT(DISTINCT d.doctor_id) AS total " + join_sql + where_sql,
        tuple(values),
    )
    total = int(count_rows[0]["total"]) if count_rows else 0

    order_by = {
        "relevance": "d.last_name ASC, d.first_name ASC",
        "name": "d.last_name ASC, d.first_name ASC",
        "lowest_fee": "lowest_fee ASC, d.last_name ASC, d.first_name ASC",
        "experience": "d.experience_years DESC, d.last_name ASC, d.first_name ASC",
    }[criteria["sort"]]
    order_values: list[Any] = []
    if criteria["sort"] == "relevance" and "name" in filters:
        order_by = "CASE WHEN CONCAT_WS(' ', d.first_name, d.last_name) LIKE %s THEN 0 ELSE 1 END, " + order_by
        order_values.append(f"{filters['name']}%")

    rows = execute_query(
        """
        SELECT d.doctor_id,
               CONCAT_WS(' ', d.first_name, d.last_name) AS name,
               d.qualification AS qualifications,
               d.experience_years AS years_of_experience,
               d.languages,
               h.name AS hospital_name,
               h.address AS hospital_address,
               h.city,
               GROUP_CONCAT(DISTINCT s.name ORDER BY s.name SEPARATOR '\n') AS specialisations,
               MIN(ct.fee) AS lowest_fee,
               d.profile_image_key
        """ + join_sql + where_sql + " GROUP BY d.doctor_id, d.first_name, d.last_name, d.qualification, d.experience_years, d.languages, h.name, h.address, h.city, d.profile_image_key "
        + f" ORDER BY {order_by} LIMIT %s OFFSET %s",
        tuple(values + order_values + [criteria["page_size"], (criteria["page"] - 1) * criteria["page_size"]]),
    )
    doctors = []
    for row in rows:
        doctors.append(
            {
                "doctor_id": row["doctor_id"],
                "name": row["name"],
                "specialisation": [part for part in (row.get("specialisations") or "").split("\n") if part],
                "qualifications": row.get("qualifications"),
                "years_of_experience": int(row.get("years_of_experience") or 0),
                "hospital": row.get("hospital_name"),
                "hospital_address": row.get("hospital_address"),
                "city": row.get("city"),
                "languages": _parse_languages(row.get("languages")),
                "photo_url": _photo_url(row.get("profile_image_key")),
                "lowest_fee": row.get("lowest_fee"),
            }
        )
    return 200, {
        "items": doctors,
        "total": total,
        "page": criteria["page"],
        "page_size": criteria["page_size"],
        "filters_used": sorted(filters),
    }


def _parse_languages(raw: Any) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(language).strip() for language in raw if str(language).strip()]
    return [language.strip() for language in str(raw).split(",") if language.strip()]


def _photo_url(key: Any) -> str | None:
    if not key or not PROFILE_PHOTOS_BUCKET:
        return None
    return get_s3_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": PROFILE_PHOTOS_BUCKET, "Key": str(key)},
        ExpiresIn=PROFILE_PHOTO_URL_TTL_SECONDS,
    )


def _doctor_options() -> dict[str, list[str]]:
    specialisations = execute_query(
        "SELECT name FROM specializations ORDER BY name"
    )
    cities = execute_query(
        "SELECT DISTINCT city FROM hospitals WHERE city <> '' ORDER BY city"
    )
    return {
        "specialisations": [row["name"] for row in specialisations],
        "cities": [row["city"] for row in cities],
        "max_page_size": MAX_PAGE_SIZE,
    }


def _doctor_profile(doctor_id: str) -> dict[str, Any] | None:
    rows = execute_query(
        """
        SELECT d.doctor_id, CONCAT_WS(' ', d.first_name, d.last_name) AS name,
               d.qualification AS qualifications, d.experience_years AS years_of_experience,
             d.languages, d.timezone, d.profile_image_key, h.name AS hospital,
               h.address AS hospital_address, h.city
        FROM doctors d
        JOIN hospitals h ON h.hospital_id = d.hospital_id
        WHERE d.doctor_id = %s AND d.is_verified = TRUE
        LIMIT 1
        """,
        (doctor_id,),
    )
    if not rows:
        return None
    row = rows[0]
    specialisations = execute_query(
        """
        SELECT s.name
        FROM doctor_specializations ds
        JOIN specializations s ON s.specialization_id = ds.specialization_id
        WHERE ds.doctor_id = %s
        ORDER BY s.name
        """,
        (doctor_id,),
    )
    consultations = execute_query(
        """
        SELECT ct.consultation_type_id AS id, catalog.name,
               catalog.duration_minutes AS duration, ct.fee, ct.capacity
        FROM consultation_types ct
        JOIN consultation_catalog catalog ON catalog.consultation_type_id = ct.catalog_type_id
        WHERE ct.doctor_id = %s AND ct.is_active = TRUE
        ORDER BY catalog.duration_minutes, catalog.name
        """,
        (doctor_id,),
    )
    return {
        "doctor_id": row["doctor_id"],
        "name": row["name"],
        "specialisation": [item["name"] for item in specialisations],
        "qualifications": row.get("qualifications"),
        "years_of_experience": int(row.get("years_of_experience") or 0),
        "languages": _parse_languages(row.get("languages")),
        "timezone": row.get("timezone") or "UTC",
        "hospital": row.get("hospital"),
        "hospital_address": row.get("hospital_address"),
        "city": row.get("city"),
        "photo_url": _photo_url(row.get("profile_image_key")),
        "consultations": consultations,
        "availability_window_days": SLOT_WINDOW_DAYS,
        "max_slot_range_days": MAX_SLOT_RANGE_DAYS,
    }


def _doctor_slots(doctor_id: str, params: dict[str, str]) -> tuple[int, dict[str, Any]]:
    from_value, to_value = params.get("from", ""), params.get("to", "")
    errors: dict[str, str] = {}
    try:
        from_day = date.fromisoformat(from_value)
    except ValueError:
        from_day = None
        errors["from"] = "Use YYYY-MM-DD format."
    try:
        to_day = date.fromisoformat(to_value)
    except ValueError:
        to_day = None
        errors["to"] = "Use YYYY-MM-DD format."
    if errors:
        return 400, {"message": "Check the date range.", "field_errors": errors}
    if to_day < from_day:
        return 400, {"message": "The to date must be on or after the from date.", "field_errors": {"to": "Date range is reversed."}}
    range_days = (to_day - from_day).days + 1
    if MAX_SLOT_RANGE_DAYS <= 0 or range_days > MAX_SLOT_RANGE_DAYS:
        return 400, {"message": "Requested date range exceeds the configured maximum."}
    today = datetime.now(timezone.utc).date()
    last_day = today + timedelta(days=max(SLOT_WINDOW_DAYS - 1, 0))
    if from_day < today or to_day > last_day:
        return 400, {"message": "Requested dates are outside the current slot window."}

    doctor_rows = execute_query(
        "SELECT timezone FROM doctors WHERE doctor_id = %s AND is_verified = TRUE LIMIT 1",
        (doctor_id,),
    )
    if not doctor_rows:
        return 404, {"message": "Doctor not found."}
    doctor_timezone = doctor_rows[0].get("timezone") or "UTC"

    type_id = params.get("consultationTypeId", "").strip()
    types = execute_query(
        """
        SELECT ct.consultation_type_id, catalog.name
        FROM consultation_types ct
        JOIN consultation_catalog catalog ON catalog.consultation_type_id = ct.catalog_type_id
        WHERE ct.doctor_id = %s AND ct.is_active = TRUE
        """,
        (doctor_id,),
    )
    type_names = {str(item["consultation_type_id"]): item["name"] for item in types}
    if type_id and type_id not in type_names:
        return 400, {"message": "Consultation type is not available for this doctor.", "field_errors": {"consultationTypeId": "Choose an active consultation type."}}

    start_key = f"{from_day.isoformat()}T00:00:00Z"
    end_key = f"{(to_day + timedelta(days=1)).isoformat()}T00:00:00Z"
    table = get_slots_table()
    query_args: dict[str, Any] = {
        "IndexName": "gsi_doctor_time",
        "KeyConditionExpression": Key("doctor_id").eq(doctor_id) & Key("start_time").between(start_key, end_key),
    }
    now_utc = datetime.now(timezone.utc)
    slots: list[dict[str, Any]] = []
    unavailable_dates: set[str] = set()
    dates_with_slots: set[str] = set()
    while True:
        result = table.query(**query_args)
        for item in result.get("Items", []):
            raw_start = item.get("start_time")
            try:
                start = datetime.fromisoformat(str(raw_start).replace("Z", "+00:00"))
            except ValueError:
                continue
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            if start.astimezone(timezone.utc).date() > to_day:
                continue
            if start.astimezone(timezone.utc) < now_utc:
                continue
            if type_id and str(item.get("consultation_type_id")) != type_id:
                continue
            day_key = start.astimezone(timezone.utc).date().isoformat()
            active = bool(item.get("is_active", True)) and not bool(item.get("is_blocked", False))
            if not active:
                if item.get("inactive_reason") == "BLACKOUT":
                    unavailable_dates.add(day_key)
                continue
            dates_with_slots.add(day_key)
            capacity = int(item.get("capacity") or 0)
            booked = int(item.get("booked_count") or 0)
            free_seats = max(0, capacity - booked)
            end_raw = item.get("end_time")
            epoch = int(start.astimezone(timezone.utc).timestamp())
            type_value = str(item.get("consultation_type_id") or "")
            slots.append(
                {
                    "slotId": f"{type_value}#{epoch}",
                    "start": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "end": str(end_raw),
                    "doctor_timezone": doctor_timezone,
                    "consultation_type": {"id": type_value, "name": type_names.get(type_value, "Consultation")},
                    "capacity": capacity,
                    "booked_count": booked,
                    "free_seats": free_seats,
                    "available": free_seats > 0,
                }
            )
        last_key = result.get("LastEvaluatedKey")
        if not last_key:
            break
        query_args["ExclusiveStartKey"] = last_key

    blackout_rows = execute_query(
        """
        SELECT start_datetime, end_datetime
        FROM blackout_dates
        WHERE doctor_id = %s AND start_datetime < %s AND end_datetime > %s
        """,
        (
            doctor_id,
            datetime.combine(to_day + timedelta(days=1), time_type.min),
            datetime.combine(from_day, time_type.min),
        ),
    )
    blackout_dates = set(unavailable_dates)
    for blackout in blackout_rows:
        start = blackout["start_datetime"].date()
        end = blackout["end_datetime"].date()
        current = max(start, from_day)
        last = min(end, to_day)
        while current <= last:
            blackout_dates.add(current.isoformat())
            current += timedelta(days=1)
    for offset in range(range_days):
        current_day = (from_day + timedelta(days=offset)).isoformat()
        if current_day not in dates_with_slots:
            unavailable_dates.add(current_day)

    slots.sort(key=lambda slot: (slot["start"], slot["consultation_type"]["id"]))
    return 200, {
        "from": from_day.isoformat(),
        "to": to_day.isoformat(),
        "doctor_timezone": doctor_timezone,
        "max_range_days": MAX_SLOT_RANGE_DAYS,
        "slots": slots,
        "blackout_dates": sorted(blackout_dates),
        "unavailable_dates": sorted(unavailable_dates),
    }


def _route(event: dict[str, Any]) -> tuple[int, dict[str, Any], str, str | None]:
    method, path, parts = _path_and_method(event)
    params = _query_params(event)
    is_public = _is_public_route(method, parts)
    user_id = None
    if not is_public:
        user_id, auth_error = _require_patient(event)
        if auth_error:
            return auth_error, {"message": "Authentication required." if auth_error == 401 else "Patient role required."}, path, None

    if method == "GET" and path == "/patients/me":
        rows = _profile_rows(user_id)
        if not rows:
            return 404, {"message": "Patient profile not found."}, path, user_id
        return 200, {"profile": _public_patient_profile(rows[0])}, path, user_id

    if method == "PUT" and path == "/patients/me":
        payload, errors = _validate_profile(_body(event))
        if errors:
            return 400, {"message": "Check the highlighted profile fields.", "field_errors": errors}, path, user_id
        with database_transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE patients SET first_name = %s, last_name = %s, date_of_birth = %s,
                        gender = %s, phone_number = %s, city = %s,
                        emergency_contact_name = %s, emergency_contact_phone = %s, notes = %s
                    WHERE user_id = %s
                    """,
                    (
                        payload["first_name"], payload["last_name"], payload["date_of_birth"],
                        payload["gender"], payload["phone"], payload["city"],
                        payload["emergency_contact_name"], payload["emergency_contact_phone"],
                        payload["notes"], user_id,
                    ),
                )
                if cursor.rowcount == 0:
                    return 404, {"message": "Patient profile not found."}, path, user_id
        rows = _profile_rows(user_id)
        return 200, {"profile": _public_patient_profile(rows[0])}, path, user_id

    if method == "DELETE" and path == "/patients/me":
        rows = execute_query(
            "SELECT patient_id FROM patients WHERE user_id = %s LIMIT 1", (user_id,)
        )
        if not rows:
            return 404, {"message": "Patient profile not found."}, path, user_id
        appointments = execute_query(
            "SELECT COUNT(*) AS count FROM appointments WHERE patient_id = %s",
            (rows[0]["patient_id"],),
        )
        if appointments and int(appointments[0]["count"]):
            return 409, {"message": "This account has appointment records and cannot be deleted in this milestone."}, path, user_id
        deleted_tokens = _delete_user_refresh_tokens(user_id)
        with database_transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM users WHERE user_id = %s AND role = 'PATIENT'",
                    (user_id,),
                )
                if cursor.rowcount != 1:
                    return 404, {"message": "Patient profile not found."}, path, user_id
        return 200, {"message": "Patient account deleted.", "revoked_sessions": deleted_tokens}, path, user_id

    if method == "GET" and path == "/doctors/options":
        return 200, _doctor_options(), path, None

    if method == "GET" and path == "/doctors":
        return _doctor_search(params) + (path, None)

    if method == "GET" and len(parts) == 2 and parts[0] == "doctors":
        profile = _doctor_profile(parts[1])
        if not profile:
            return 404, {"message": "Doctor not found."}, path, None
        return 200, {"doctor": profile}, path, None

    if method == "GET" and len(parts) == 3 and parts[0] == "doctors" and parts[2] == "slots":
        status, payload = _doctor_slots(parts[1], params)
        return status, payload, path, user_id

    return 404, {"message": "Route not found."}, path, user_id


def _body(event: dict[str, Any]) -> Any:
    body = event.get("body")
    if isinstance(body, dict):
        return body
    if not isinstance(body, str) or not body:
        return {}
    if event.get("isBase64Encoded"):
        import base64
        body = base64.b64decode(body).decode("utf-8")
    try:
        return json.loads(body)
    except (TypeError, ValueError, UnicodeDecodeError):
        return None


def _handle_patient_request(event: dict[str, Any] | None, context: Any = None) -> dict[str, Any]:
    event = event if isinstance(event, dict) else {}
    correlation_id = get_correlation_id(event, context)
    user_id = _patient_user_id(event)
    try:
        status, payload, route, resolved_user_id = _route(event)
    except Exception as exc:
        log_exception(
            "patient_request_failed",
            "Patient API request failed unexpectedly",
            correlation_id,
            error_type=type(exc).__name__,
            error_message="Internal request failure",
            user_id=user_id,
            role="patient" if user_id else None,
        )
        status, payload, route, resolved_user_id = (
            500,
            {"message": "An unexpected error occurred."},
            "/patients",
            user_id,
        )

    if route == "/doctors" and status == 200 and isinstance(payload.get("items"), list):
        query_params = _query_params(event)
        filters_used = sorted(
            name
            for name in ("city", "hospital", "name", "specialisation")
            if query_params.get(name)
        )
        log_info(
            "doctor_search_completed",
            "Doctor search completed",
            correlation_id,
            filters_used=filters_used,
            result_count=len(payload["items"]),
            page=payload.get("page"),
            page_size=payload.get("page_size"),
        )

    if status in {400, 401, 403, 404, 405, 409}:
        field_errors = payload.get("field_errors") or {}
        log_warning(
            "patient_request_rejected",
            "Patient API request was rejected",
            correlation_id,
            http_status=status,
            field_names=sorted(field_errors),
            user_id=resolved_user_id,
            role="patient" if resolved_user_id else None,
        )
    return _response(status, payload, correlation_id)


def lambda_handler(event: dict[str, Any] | None, context: Any = None) -> dict[str, Any]:
    """Contain failures that happen before normal patient request routing."""
    event = event if isinstance(event, dict) else {}
    correlation_id = get_correlation_id(event, context)
    try:
        method, _, _ = _path_and_method(event)
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
        or "/patients"
    )
    started = time.perf_counter()
    log_info(
        "patient_request_started",
        "Patient API request started",
        correlation_id,
        method=method,
        route=route,
    )
    try:
        result = _handle_patient_request(event, context)
    except Exception as exc:
        log_exception(
            "patient_request_failed",
            "Patient API request failed unexpectedly",
            correlation_id,
            error_type=type(exc).__name__,
            error_message="Internal request failure",
        )
        result = _response(
            500,
            {"message": "An unexpected error occurred."},
            correlation_id,
        )
    log_info(
        "patient_request_finished",
        "Patient API request finished",
        correlation_id,
        http_status=int(result.get("statusCode", 500)),
        duration_ms=int((time.perf_counter() - started) * 1000),
    )
    return result