"""
MediSlot Authentication Handler

Responsibilities:
    - Doctor registration
    - Patient registration
    - Login
    - Access-token generation
    - Refresh-token rotation
    - Logout and access-token revocation

Security:
    - Passwords hashed with bcrypt.
    - JWT algorithm: HS256.
    - Access-token lifetime: 15 minutes.
    - Refresh-token lifetime: 7 days.
    - Only SHA-256 hashes of refresh tokens are stored in DynamoDB.
    - JWT signing secret is retrieved from SSM SecureString.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import boto3
import bcrypt
import jwt
import pymysql
from botocore.exceptions import ClientError

from shared.database import get_database_connection


# ============================================================
# LOGGING
# ============================================================

logger = logging.getLogger()
logger.setLevel(logging.INFO)


# ============================================================
# CONFIGURATION
# ============================================================

AWS_REGION = os.environ.get("AWS_REGION", "ap-south-1")

ENVIRONMENT = os.environ.get("ENVIRONMENT", "dev")

JWT_SECRET_PARAMETER_NAME = os.environ.get(
    "JWT_SECRET_PARAMETER_NAME", ""
)

# The CloudFormation stack sets REFRESH_TOKENS_TABLE; the older
# REFRESH_TOKENS_TABLE_NAME name is still honoured if present.
REFRESH_TOKENS_TABLE_NAME = (
    os.environ.get("REFRESH_TOKENS_TABLE_NAME")
    or os.environ.get("REFRESH_TOKENS_TABLE")
    or f"MediSlot-RefreshTokens-{ENVIRONMENT}"
)

TOKEN_DENYLIST_TABLE_NAME = os.environ.get(
    "TOKEN_DENYLIST_TABLE_NAME",
    f"MediSlot-TokenDenylist-{ENVIRONMENT}",
)

BCRYPT_WORK_FACTOR = int(
    os.environ.get("BCRYPT_WORK_FACTOR", "12")
)

PASSWORD_MIN_LENGTH = int(
    os.environ.get("PASSWORD_MIN_LENGTH", "10")
)

ACCESS_TOKEN_LIFETIME_SECONDS = 15 * 60
REFRESH_TOKEN_LIFETIME_SECONDS = 7 * 24 * 60 * 60

ALLOWED_ROLES = {"doctor", "patient"}

ALLOWED_GENDERS = {"female", "male", "other", "undisclosed"}

ssm_client = boto3.client("ssm", region_name=AWS_REGION)
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)

refresh_tokens_table = dynamodb.Table(
    REFRESH_TOKENS_TABLE_NAME
)

token_denylist_table = dynamodb.Table(
    TOKEN_DENYLIST_TABLE_NAME
)

_cached_jwt_secret = None


# ============================================================
# RESPONSE HELPERS
# ============================================================

def response(
    status_code: int,
    body: dict[str, Any],
) -> dict[str, Any]:
    """Return a JSON response suitable for API Gateway."""

    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
        },
        "body": json.dumps(body, default=str),
    }


def get_request_body(event: dict[str, Any]) -> dict[str, Any]:
    """Parse the API Gateway request body."""

    body = event.get("body")

    if isinstance(body, dict):
        return body

    if not body:
        return {}

    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")

    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
        raise ValueError("Request body must contain valid JSON.")

    if not isinstance(parsed, dict):
        raise ValueError("Request body must be a JSON object.")

    return parsed


def get_http_method(event: dict[str, Any]) -> str:
    """Support REST API and HTTP API event formats."""

    return (
        event.get("httpMethod")
        or event.get("requestContext", {})
        .get("http", {})
        .get("method")
        or ""
    ).upper()


def get_path(event: dict[str, Any]) -> str:
    """Get the normalized request path."""

    path = (
        event.get("path")
        or event.get("rawPath")
        or event.get("requestContext", {})
        .get("http", {})
        .get("path")
        or ""
    )

    if path != "/":
        path = path.rstrip("/")

    return path.lower()


def get_header(
    event: dict[str, Any],
    name: str,
) -> str | None:
    """Read a request header without depending on its casing."""

    headers = event.get("headers") or {}
    target = name.lower()

    for key, value in headers.items():
        if key.lower() == target:
            return value

    return None


def get_correlation_id(event: dict[str, Any]) -> str:
    """Get a request identifier for structured log correlation."""

    return (
        event.get("requestContext", {}).get("requestId")
        or str(uuid.uuid4())
    )


# ============================================================
# JWT SECRET
# ============================================================

def get_jwt_secret() -> str:
    """
    Retrieve and cache the JWT signing secret from SSM.

    The parameter must be a SecureString. Its value is never logged.
    """

    global _cached_jwt_secret

    if _cached_jwt_secret is not None:
        return _cached_jwt_secret

    if not JWT_SECRET_PARAMETER_NAME:
        raise RuntimeError(
            "JWT_SECRET_PARAMETER_NAME is not configured."
        )

    result = ssm_client.get_parameter(
        Name=JWT_SECRET_PARAMETER_NAME,
        WithDecryption=True,
    )

    secret = result["Parameter"]["Value"]

    if not secret or len(secret) < 32:
        raise RuntimeError(
            "JWT signing secret must contain at least 32 characters."
        )

    _cached_jwt_secret = secret

    return _cached_jwt_secret


# ============================================================
# VALIDATION
# ============================================================

def normalize_email(email: Any) -> str:
    """Normalize and validate an email address."""

    if not isinstance(email, str):
        raise ValueError("A valid email address is required.")

    email = email.strip().lower()

    if (
        len(email) > 254
        or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)
    ):
        raise ValueError("A valid email address is required.")

    return email


def validate_password(password: Any) -> str:
    """Validate the password policy."""

    if not isinstance(password, str):
        raise ValueError("A password is required.")

    if len(password) < PASSWORD_MIN_LENGTH:
        raise ValueError(
            f"Password must contain at least "
            f"{PASSWORD_MIN_LENGTH} characters."
        )

    if not re.search(r"[A-Za-z]", password):
        raise ValueError("Password must contain a letter.")

    if not re.search(r"\d", password):
        raise ValueError("Password must contain a digit.")

    # bcrypt has a maximum input length of 72 bytes.
    if len(password.encode("utf-8")) > 72:
        raise ValueError(
            "Password exceeds the supported bcrypt input length."
        )

    return password


def required_text(
    body: dict[str, Any],
    field: str,
    max_length: int,
) -> str:
    """Return a required, trimmed string field."""

    value = body.get(field)

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required.")

    value = value.strip()

    if len(value) > max_length:
        raise ValueError(
            f"{field} must not exceed {max_length} characters."
        )

    return value


def optional_text(
    body: dict[str, Any],
    field: str,
    max_length: int,
) -> str | None:
    """Return an optional, trimmed string field or None."""

    value = body.get(field)

    if value is None or value == "":
        return None

    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string.")

    value = value.strip()

    if len(value) > max_length:
        raise ValueError(
            f"{field} must not exceed {max_length} characters."
        )

    return value or None


def validate_phone(value: Any, field: str) -> str:
    """Validate a phone number (digits, spaces, hyphens, optional +)."""

    if not isinstance(value, str) or not re.fullmatch(
        r"\+?[0-9][0-9\s\-]{6,19}", value.strip()
    ):
        raise ValueError(
            f"{field} must be a valid phone number "
            f"(7 to 20 digits, optional leading +)."
        )

    return value.strip()


def validate_patient_profile(body: dict[str, Any]) -> dict[str, Any]:
    """Validate the patient-specific registration fields."""

    date_of_birth = body.get("date_of_birth")

    if not isinstance(date_of_birth, str):
        raise ValueError(
            "date_of_birth is required in YYYY-MM-DD format."
        )

    try:
        dob = datetime.strptime(
            date_of_birth.strip(),
            "%Y-%m-%d",
        ).date()
    except ValueError:
        raise ValueError(
            "date_of_birth must be a valid date in YYYY-MM-DD format."
        )

    if dob >= datetime.now(timezone.utc).date() or dob.year < 1900:
        raise ValueError(
            "date_of_birth must be a past date after 1900."
        )

    gender = body.get("gender")

    if not isinstance(gender, str) or (
        gender.strip().lower() not in ALLOWED_GENDERS
    ):
        raise ValueError(
            "gender must be one of: "
            + ", ".join(sorted(ALLOWED_GENDERS))
            + "."
        )

    emergency_phone = body.get("emergency_contact_phone")

    if emergency_phone in (None, ""):
        emergency_phone = None
    else:
        emergency_phone = validate_phone(
            emergency_phone,
            "emergency_contact_phone",
        )

    return {
        "date_of_birth": dob.isoformat(),
        "gender": gender.strip().lower(),
        "phone": validate_phone(body.get("phone"), "phone"),
        "city": required_text(body, "city", 100),
        "emergency_contact_name": optional_text(
            body,
            "emergency_contact_name",
            150,
        ),
        "emergency_contact_phone": emergency_phone,
    }


def validate_doctor_profile(body: dict[str, Any]) -> dict[str, Any]:
    """Validate the doctor-specific registration fields."""

    licence_number = required_text(body, "licence_number", 50)

    specialisation_id = body.get("specialisation_id")

    if (
        isinstance(specialisation_id, bool)
        or not isinstance(specialisation_id, int)
        or specialisation_id < 1
    ):
        raise ValueError(
            "specialisation_id must be a positive integer."
        )

    hospital_id = required_text(body, "hospital_id", 36)

    try:
        hospital_id = str(uuid.UUID(hospital_id))
    except ValueError:
        raise ValueError("hospital_id must be a valid UUID.")

    years_experience = body.get("years_experience", 0)

    if (
        isinstance(years_experience, bool)
        or not isinstance(years_experience, int)
        or not 0 <= years_experience <= 255
    ):
        raise ValueError(
            "years_experience must be an integer from 0 to 255."
        )

    timezone_name = required_text(body, "timezone", 64)

    # IANA zone format, e.g. Asia/Kolkata or America/Argentina/Buenos_Aires.
    if timezone_name != "UTC" and not re.fullmatch(
        r"[A-Za-z_]+(?:/[A-Za-z0-9_+\-]+){1,2}",
        timezone_name,
    ):
        raise ValueError(
            "timezone must be an IANA timezone such as Asia/Kolkata."
        )

    return {
        "licence_number": licence_number,
        "specialisation_id": specialisation_id,
        "hospital_id": hospital_id,
        "years_experience": years_experience,
        "qualifications": optional_text(body, "qualifications", 500),
        "timezone": timezone_name,
    }


def hash_password(password: str) -> str:
    """Hash a password with the configured bcrypt work factor."""

    if BCRYPT_WORK_FACTOR < 12:
        raise RuntimeError(
            "BCRYPT_WORK_FACTOR must be at least 12."
        )

    password_bytes = password.encode("utf-8")

    salt = bcrypt.gensalt(rounds=BCRYPT_WORK_FACTOR)

    return bcrypt.hashpw(
        password_bytes,
        salt,
    ).decode("utf-8")


def verify_password(
    password: str,
    stored_hash: str,
) -> bool:
    """Verify a password without logging its value."""

    try:
        return bcrypt.checkpw(
            password.encode("utf-8"),
            stored_hash.encode("utf-8"),
        )
    except (ValueError, TypeError):
        return False


# ============================================================
# TOKEN HELPERS
# ============================================================

def hash_refresh_token(refresh_token: str) -> str:
    """Return the SHA-256 hash used as the DynamoDB partition key."""

    return hashlib.sha256(
        refresh_token.encode("utf-8")
    ).hexdigest()


def create_refresh_token() -> str:
    """Generate an opaque, cryptographically random refresh token."""

    return secrets.token_urlsafe(48)


def create_access_token(
    user_id: str,
    role: str,
) -> tuple[str, dict[str, Any]]:
    """
    Create a signed JWT with exactly the required claims:
    sub, role, iat, exp, jti.
    """

    now = int(time.time())

    claims = {
        "sub": str(user_id),
        "role": role,
        "iat": now,
        "exp": now + ACCESS_TOKEN_LIFETIME_SECONDS,
        "jti": str(uuid.uuid4()),
    }

    token = jwt.encode(
        claims,
        get_jwt_secret(),
        algorithm="HS256",
    )

    return token, claims


def decode_access_token(
    token: str,
    verify_expiration: bool = True,
) -> dict[str, Any]:
    """Validate an access token's signature and required claims."""

    claims = jwt.decode(
        token,
        get_jwt_secret(),
        algorithms=["HS256"],
        options={
            "verify_signature": True,
            "verify_exp": verify_expiration,
            "require": [
                "sub",
                "role",
                "iat",
                "exp",
                "jti",
            ],
        },
    )

    if set(claims.keys()) != {
        "sub",
        "role",
        "iat",
        "exp",
        "jti",
    }:
        raise jwt.InvalidTokenError("Unexpected JWT claims.")

    if claims["role"] not in ALLOWED_ROLES:
        raise jwt.InvalidTokenError("Invalid token role.")

    if not isinstance(claims["sub"], str) or not claims["sub"]:
        raise jwt.InvalidTokenError("Invalid subject.")

    if not isinstance(claims["jti"], str) or not claims["jti"]:
        raise jwt.InvalidTokenError("Invalid token identifier.")

    if (
        isinstance(claims["iat"], bool)
        or not isinstance(claims["iat"], int)
        or isinstance(claims["exp"], bool)
        or not isinstance(claims["exp"], int)
    ):
        raise jwt.InvalidTokenError("Invalid token timestamps.")

    now = int(time.time())

    if claims["iat"] > now:
        raise jwt.InvalidTokenError("Token issued in the future.")

    if claims["exp"] <= claims["iat"]:
        raise jwt.InvalidTokenError("Invalid token lifetime.")

    if claims["exp"] - claims["iat"] > ACCESS_TOKEN_LIFETIME_SECONDS:
        raise jwt.InvalidTokenError("Token lifetime exceeds policy.")

    return claims


def get_bearer_token(event: dict[str, Any]) -> str:
    """Extract a Bearer access token from the Authorization header."""

    authorization = get_header(event, "Authorization")

    if not authorization:
        raise ValueError("Authorization header is required.")

    parts = authorization.strip().split()

    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise ValueError("A valid Bearer access token is required.")

    return parts[1]


# ============================================================
# DYNAMODB REFRESH TOKEN HELPERS
# ============================================================

def store_refresh_token(
    refresh_token: str,
    user_id: str,
    role: str,
) -> dict[str, Any]:
    """
    Store only the refresh-token hash.

    Attribute names match the MediSlot refresh-token table:
    tokenHash, userId, role, issuedAt, expiresAt, ttl.
    """

    now = int(time.time())

    expires_at = now + REFRESH_TOKEN_LIFETIME_SECONDS

    issued_at = datetime.fromtimestamp(
        now,
        tz=timezone.utc,
    ).isoformat()

    item = {
        "tokenHash": hash_refresh_token(refresh_token),
        "userId": str(user_id),
        "role": role,
        "issuedAt": issued_at,
        "expiresAt": expires_at,
        "ttl": expires_at,
    }

    refresh_tokens_table.put_item(
        Item=item,
        ConditionExpression="attribute_not_exists(tokenHash)",
    )

    return item


def find_refresh_token(
    refresh_token: str,
) -> dict[str, Any] | None:
    """Look up a refresh token by its hash."""

    result = refresh_tokens_table.get_item(
        Key={
            "tokenHash": hash_refresh_token(refresh_token),
        },
        ConsistentRead=True,
    )

    return result.get("Item")


def revoke_refresh_token(refresh_token: str) -> bool:
    """
    Delete a refresh token using a conditional write.

    Returns False when the token is missing or has already been consumed.
    """

    token_hash = hash_refresh_token(refresh_token)

    try:
        refresh_tokens_table.delete_item(
            Key={"tokenHash": token_hash},
            ConditionExpression="attribute_exists(tokenHash)",
        )

        return True

    except ClientError as exc:
        if (
            exc.response.get("Error", {}).get("Code")
            == "ConditionalCheckFailedException"
        ):
            return False

        raise

def rotate_refresh_token(
    old_refresh_token: str,
    token_item: dict[str, Any],
) -> tuple[str, str, dict[str, Any]]:
    """
    Atomically consume an existing refresh token and create its replacement.
    """

    now = int(time.time())

    try:
        old_expires_at = int(token_item["expiresAt"])
        user_id = str(token_item["userId"])
        role = str(token_item["role"])

        if not user_id or not role:
            raise ValueError("Missing user information.")

    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        logger.warning(
            "Refresh-token rotation rejected because the stored record "
            "is malformed."
        )
        raise ValueError(
            "Refresh token is invalid or expired."
        ) from exc

    if old_expires_at <= now:
        raise ValueError("Refresh token is invalid or expired.")

    old_hash = hash_refresh_token(old_refresh_token)

    new_refresh_token = create_refresh_token()
    new_hash = hash_refresh_token(new_refresh_token)

    new_expires_at = now + REFRESH_TOKEN_LIFETIME_SECONDS

    issued_at = datetime.fromtimestamp(
        now,
        tz=timezone.utc,
    ).isoformat()

    access_token, claims = create_access_token(
        user_id,
        role,
    )

    client = dynamodb.meta.client

    try:
        client.transact_write_items(
            TransactItems=[
                {
                    "Delete": {
                        "TableName": REFRESH_TOKENS_TABLE_NAME,
                        "Key": {
                            "tokenHash": {
                                "S": old_hash
                            }
                        },
                        "ConditionExpression": (
                            "attribute_exists(tokenHash) "
                            "AND userId = :user_id "
                            "AND expiresAt = :expected_expiry"
                        ),
                        "ExpressionAttributeValues": {
                            ":user_id": {
                                "S": user_id
                            },
                            ":expected_expiry": {
                                "N": str(old_expires_at)
                            }
                        }
                    }
                },
                {
                    "Put": {
                        "TableName": REFRESH_TOKENS_TABLE_NAME,
                        "Item": {
                            "tokenHash": {
                                "S": new_hash
                            },
                            "userId": {
                                "S": user_id
                            },
                            "role": {
                                "S": role
                            },
                            "issuedAt": {
                                "S": issued_at
                            },
                            "expiresAt": {
                                "N": str(new_expires_at)
                            },
                            "ttl": {
                                "N": str(new_expires_at)
                            }
                        },
                        "ConditionExpression": (
                            "attribute_not_exists(tokenHash)"
                        )
                    }
                }
            ]
        )

    except ClientError as exc:
        error_code = exc.response.get(
            "Error",
            {}
        ).get(
            "Code",
            "Unknown"
        )

        logger.warning(
            "Refresh-token rotation transaction failed. "
            "error_code=%s",
            error_code,
        )

        if error_code in (
            "TransactionCanceledException",
            "ConditionalCheckFailedException",
        ):
            raise ValueError(
                "Refresh token is invalid, expired, or already used."
            ) from exc

        raise

    logger.info(
        "Refresh-token rotation succeeded. user_id=%s role=%s",
        user_id,
        role,
    )

    return (
        access_token,
        new_refresh_token,
        claims,
    )

# ============================================================
# DATABASE HELPERS
# ============================================================

def find_user_by_email(
    email: str,
    correlation_id: str | None = None,
) -> dict[str, Any] | None:
    """Retrieve a user's identity and password hash from RDS with timing logs."""

    started = time.perf_counter()
    logger.info(
        "Login stage started. stage=rds_connect correlation_id=%s",
        correlation_id or "not_set",
    )
    connection = get_database_connection()
    logger.info(
        "Login stage completed. stage=rds_connect correlation_id=%s duration_ms=%d",
        correlation_id or "not_set",
        int((time.perf_counter() - started) * 1000),
    )

    try:
        query_started = time.perf_counter()
        logger.info(
            "Login stage started. stage=rds_user_query correlation_id=%s",
            correlation_id or "not_set",
        )
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    ui.email,
                    ui.role,
                    ui.user_id,
                    CASE
                        WHEN ui.role = 'doctor' THEN d.password_hash
                        WHEN ui.role = 'patient' THEN p.password_hash
                    END AS password_hash
                FROM user_identities AS ui
                LEFT JOIN doctors AS d
                    ON ui.role = 'doctor' AND d.id = ui.user_id
                LEFT JOIN patients AS p
                    ON ui.role = 'patient' AND p.id = ui.user_id
                WHERE ui.email = %s
                LIMIT 1
                """,
                (email,),
            )
            row = cursor.fetchone()

        logger.info(
            "Login stage completed. stage=rds_user_query correlation_id=%s "
            "found_user=%s duration_ms=%d",
            correlation_id or "not_set",
            bool(row),
            int((time.perf_counter() - query_started) * 1000),
        )
        if not row:
            return None
        if isinstance(row, dict):
            return row
        return {
            "email": row[0],
            "role": row[1],
            "user_id": row[2],
            "password_hash": row[3],
        }
    finally:
        connection.close()
        logger.info(
            "Login stage completed. stage=rds_connection_closed correlation_id=%s",
            correlation_id or "not_set",
        )


def get_connection():
    """Create a database connection using the shared database helper."""

    return get_database_connection()


# ============================================================
# REGISTRATION
# ============================================================

def register_user(
    body: dict[str, Any],
    role: str,
) -> dict[str, Any]:
    """Register a doctor or patient in RDS."""

    email = normalize_email(body.get("email"))

    password = validate_password(body.get("password"))

    full_name = body.get("full_name")

    if not isinstance(full_name, str) or not full_name.strip():
        raise ValueError("full_name is required.")

    full_name = full_name.strip()

    if len(full_name) > 150:
        raise ValueError("full_name must not exceed 150 characters.")

    # Validate all role-specific input before any hashing or database work.
    if role == "doctor":
        profile = validate_doctor_profile(body)
    else:
        profile = validate_patient_profile(body)

    password_hash = hash_password(password)

    user_id = str(uuid.uuid4())

    connection = get_connection()

    try:
        connection.begin()

        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO user_identities (
                    email,
                    role,
                    user_id
                )
                VALUES (%s, %s, %s)
                """,
                (email, role, user_id),
            )

            if role == "doctor":
                cursor.execute(
                    """
                    INSERT INTO doctors (
                        id,
                        email,
                        password_hash,
                        full_name,
                        licence_number,
                        specialisation_id,
                        hospital_id,
                        years_experience,
                        qualifications,
                        timezone
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        user_id,
                        email,
                        password_hash,
                        full_name,
                        profile["licence_number"],
                        profile["specialisation_id"],
                        profile["hospital_id"],
                        profile["years_experience"],
                        profile["qualifications"],
                        profile["timezone"],
                    ),
                )

            else:
                cursor.execute(
                    """
                    INSERT INTO patients (
                        id,
                        email,
                        password_hash,
                        full_name,
                        date_of_birth,
                        gender,
                        phone,
                        city,
                        emergency_contact_name,
                        emergency_contact_phone
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        user_id,
                        email,
                        password_hash,
                        full_name,
                        profile["date_of_birth"],
                        profile["gender"],
                        profile["phone"],
                        profile["city"],
                        profile["emergency_contact_name"],
                        profile["emergency_contact_phone"],
                    ),
                )

        connection.commit()

        logger.info(
            "User registration succeeded. role=%s user_id=%s",
            role,
            user_id,
        )

        return {
            "statusCode": 201,
            "body": {
                "message": "Registration successful.",
                "user": {
                    "user_id": user_id,
                    "email": email,
                    "role": role,
                },
            },
        }

    except pymysql.err.IntegrityError as exc:
        connection.rollback()

        error_code = exc.args[0] if exc.args else None

        # 1062: duplicate email or licence number.
        if error_code == 1062:
            logger.info(
                "Registration rejected because of a duplicate value. "
                "role=%s",
                role,
            )

            return {
                "statusCode": 409,
                "body": {
                    "message": (
                        "An account with these details "
                        "already exists."
                    ),
                },
            }

        # 1452: foreign key failure (specialisation or hospital).
        if error_code == 1452:
            return {
                "statusCode": 400,
                "body": {
                    "message": (
                        "specialisation_id or hospital_id "
                        "does not exist."
                    ),
                },
            }

        raise

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()


# ============================================================
# LOGIN
# ============================================================

def login_user(
    body: dict[str, Any],
    expected_role: str | None = None,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """Authenticate credentials and issue tokens with timed stage logs."""

    login_started = time.perf_counter()
    correlation_id = correlation_id or "not_set"
    logger.info(
        "Login flow started. correlation_id=%s expected_role=%s",
        correlation_id,
        expected_role or "any",
    )

    stage_started = time.perf_counter()
    email = normalize_email(body.get("email"))
    password = body.get("password")
    if not isinstance(password, str):
        raise ValueError("Email and password are required.")
    logger.info(
        "Login stage completed. stage=input_validation correlation_id=%s duration_ms=%d",
        correlation_id, int((time.perf_counter() - stage_started) * 1000),
    )

    stage_started = time.perf_counter()
    user = find_user_by_email(email, correlation_id=correlation_id)
    logger.info(
        "Login stage completed. stage=rds_lookup correlation_id=%s user_found=%s duration_ms=%d",
        correlation_id, bool(user), int((time.perf_counter() - stage_started) * 1000),
    )

    stage_started = time.perf_counter()
    password_valid = bool(
        user and user.get("password_hash")
        and verify_password(password, user["password_hash"])
    )
    logger.info(
        "Login stage completed. stage=password_verification correlation_id=%s "
        "password_valid=%s duration_ms=%d",
        correlation_id, password_valid, int((time.perf_counter() - stage_started) * 1000),
    )
    if not password_valid:
        logger.info("Login rejected. reason=invalid_credentials correlation_id=%s", correlation_id)
        return {"statusCode": 401, "body": {"message": "Invalid email or password."}}

    role = user["role"]
    if expected_role is not None and role != expected_role:
        logger.info("Login rejected. reason=role_mismatch correlation_id=%s", correlation_id)
        return {"statusCode": 401, "body": {"message": "Invalid email or password."}}
    if role not in ALLOWED_ROLES:
        logger.error("Login failed because the stored role is invalid. correlation_id=%s", correlation_id)
        return {"statusCode": 401, "body": {"message": "Unable to authenticate this account."}}

    user_id = str(user["user_id"])

    stage_started = time.perf_counter()
    logger.info("Login stage started. stage=jwt_creation correlation_id=%s", correlation_id)
    access_token, claims = create_access_token(user_id, role)
    logger.info(
        "Login stage completed. stage=jwt_creation correlation_id=%s duration_ms=%d",
        correlation_id, int((time.perf_counter() - stage_started) * 1000),
    )

    stage_started = time.perf_counter()
    refresh_token = create_refresh_token()
    logger.info(
        "Login stage completed. stage=refresh_token_generation correlation_id=%s duration_ms=%d",
        correlation_id, int((time.perf_counter() - stage_started) * 1000),
    )

    stage_started = time.perf_counter()
    logger.info(
        "Login stage started. stage=dynamodb_store_refresh_token correlation_id=%s table=%s",
        correlation_id, REFRESH_TOKENS_TABLE_NAME,
    )
    store_refresh_token(refresh_token, user_id, role)
    logger.info(
        "Login stage completed. stage=dynamodb_store_refresh_token correlation_id=%s duration_ms=%d",
        correlation_id, int((time.perf_counter() - stage_started) * 1000),
    )

    logger.info(
        "Login succeeded. role=%s user_id=%s correlation_id=%s total_duration_ms=%d",
        role, user_id, correlation_id, int((time.perf_counter() - login_started) * 1000),
    )
    return {
        "statusCode": 200,
        "body": {
            "message": "Login successful.",
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_LIFETIME_SECONDS,
            "refresh_token": refresh_token,
            "refresh_expires_in": REFRESH_TOKEN_LIFETIME_SECONDS,
            "user": {"user_id": user_id, "email": email, "role": role},
        },
    }


# ============================================================
# REFRESH
# ============================================================

def refresh_session(
    body: dict[str, Any],
) -> dict[str, Any]:
    """Rotate a refresh token and issue a new access token."""

    old_refresh_token = body.get("refresh_token")

    if (
        not isinstance(old_refresh_token, str)
        or not old_refresh_token
    ):
        return {
            "statusCode": 400,
            "body": {
                "message": "refresh_token is required.",
            },
        }

    token_item = find_refresh_token(old_refresh_token)

    if not token_item:
        return {
            "statusCode": 401,
            "body": {
                "message": "Refresh token is invalid or expired.",
            },
        }

    now = int(time.time())

    try:
        expires_at = int(token_item["expiresAt"])
    except (KeyError, TypeError, ValueError):
        logger.error("Refresh token record has invalid expiry data.")

        return {
            "statusCode": 401,
            "body": {
                "message": "Refresh token is invalid or expired.",
            },
        }

    if expires_at <= now:
        # Best-effort cleanup of an expired token.
        try:
            revoke_refresh_token(old_refresh_token)
        except Exception:
            logger.warning("Expired refresh token cleanup failed.")

        return {
            "statusCode": 401,
            "body": {
                "message": "Refresh token is invalid or expired.",
            },
        }

    if token_item.get("role") not in ALLOWED_ROLES:
        return {
            "statusCode": 401,
            "body": {
                "message": "Refresh token is invalid.",
            },
        }

    try:
        access_token, new_refresh_token, claims = (
            rotate_refresh_token(
                old_refresh_token,
                token_item,
            )
        )

    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")

        if code in {
            "TransactionCanceledException",
            "ConditionalCheckFailedException",
        }:
            return {
                "statusCode": 401,
                "body": {
                    "message": (
                        "Refresh token is invalid, expired, "
                        "or has already been used."
                    ),
                },
            }

        raise

    logger.info(
        "Refresh succeeded. role=%s user_id=%s",
        token_item["role"],
        token_item["userId"],
    )

    return {
        "statusCode": 200,
        "body": {
            "message": "Token refresh successful.",
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_LIFETIME_SECONDS,
            "refresh_token": new_refresh_token,
            "refresh_expires_in": REFRESH_TOKEN_LIFETIME_SECONDS,
        },
    }


# ============================================================
# LOGOUT
# ============================================================

def logout_session(
    event: dict[str, Any],
    body: dict[str, Any],
) -> dict[str, Any]:
    """
    Revoke the access token and delete the supplied refresh token.

    The denylist TTL is set to the access token's expiry time.
    """

    refresh_token = body.get("refresh_token")

    if (
        not isinstance(refresh_token, str)
        or not refresh_token
    ):
        return {
            "statusCode": 400,
            "body": {
                "message": "refresh_token is required for logout.",
            },
        }

    access_token = get_bearer_token(event)

    try:
        claims = decode_access_token(access_token)

    except jwt.ExpiredSignatureError:
        return {
            "statusCode": 401,
            "body": {
                "message": "Access token has expired.",
            },
        }

    except jwt.InvalidTokenError:
        return {
            "statusCode": 401,
            "body": {
                "message": "Access token is invalid.",
            },
        }

    token_item = find_refresh_token(refresh_token)

    # If a refresh token exists, it must belong to the same user
    # as the access token being revoked.
    if token_item:
        if str(token_item.get("userId")) != claims["sub"]:
            return {
                "statusCode": 403,
                "body": {
                    "message": (
                        "Refresh token does not belong "
                        "to the authenticated user."
                    ),
                },
            }

    now = int(time.time())

    remaining_lifetime = claims["exp"] - now

    if remaining_lifetime <= 0:
        return {
            "statusCode": 401,
            "body": {
                "message": "Access token has expired.",
            },
        }

    # Revoke the access token. If this write fails, do not report
    # logout success.
    token_denylist_table.put_item(
        Item={
            "jti": claims["jti"],
            "ttl": claims["exp"],
        },
    )

    # Delete the refresh token after the access token is denylisted.
    # If the token was already consumed or deleted, logout remains
    # successful once the access token has been revoked.
    try:
        revoke_refresh_token(refresh_token)

    except Exception:
        logger.exception(
            "Access token revoked, but refresh-token deletion failed. "
            "user_id=%s",
            claims["sub"],
        )

        return {
            "statusCode": 500,
            "body": {
                "message": (
                    "Access token was revoked, but session cleanup "
                    "could not be completed. Please retry logout."
                ),
            },
        }

    logger.info(
        "Logout succeeded. user_id=%s",
        claims["sub"],
    )

    return {
        "statusCode": 200,
        "body": {
            "message": "Logout successful.",
        },
    }


# ============================================================
# ROUTER
# ============================================================

def lambda_handler(
    event: dict[str, Any],
    context: Any,
) -> dict[str, Any]:
    """Route authentication requests to their respective handlers."""

    correlation_id = get_correlation_id(event)

    method = get_http_method(event)
    path = get_path(event)

    logger.info(
        "Authentication request received. "
        "correlation_id=%s method=%s path=%s",
        correlation_id,
        method,
        path,
    )

    try:
        body = get_request_body(event)

        if method == "POST" and path == "/auth/doctors/register":
            result = register_user(body, "doctor")

        elif method == "POST" and path == "/auth/patients/register":
            result = register_user(body, "patient")

        elif method == "POST" and path == "/auth/doctors/login":
            result = login_user(body, expected_role="doctor", correlation_id=correlation_id)

        elif method == "POST" and path == "/auth/patients/login":
            result = login_user(body, expected_role="patient", correlation_id=correlation_id)

        # Keep the shared route for compatibility with an existing API
        # Gateway configuration while role-specific routes are available.
        elif method == "POST" and path == "/auth/login":
            result = login_user(body, correlation_id=correlation_id)

        elif method == "POST" and path == "/auth/refresh":
            result = refresh_session(body)

        elif method == "POST" and path == "/auth/logout":
            result = logout_session(event, body)

        else:
            result = {
                "statusCode": 404,
                "body": {
                    "message": "Authentication route not found.",
                },
            }

        return response(
            result["statusCode"],
            result["body"],
        )

    except ValueError as exc:
        logger.info(
            "Authentication request validation failed. "
            "correlation_id=%s reason=%s",
            correlation_id,
            str(exc),
        )

        return response(
            400,
            {"message": str(exc)},
        )

    except ClientError as exc:
        error_code = exc.response.get(
            "Error", {}
        ).get("Code", "Unknown")

        logger.exception(
            "AWS service error during authentication. "
            "correlation_id=%s error_code=%s",
            correlation_id,
            error_code,
        )

        return response(
            500,
            {
                "message": "Authentication service temporarily unavailable.",
                "correlation_id": correlation_id,
            },
        )

    except Exception:
        logger.exception(
            "Unexpected authentication failure. correlation_id=%s",
            correlation_id,
        )

        return response(
            500,
            {
                "message": "An unexpected authentication error occurred.",
                "correlation_id": correlation_id,
            },
        )