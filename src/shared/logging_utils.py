
"""
MediSlot shared logging utilities.

Features:
- Structured JSON logs.
- Correlation IDs for tracing requests.
- Consistent INFO, WARNING, ERROR, and EXCEPTION events.
- Safe logging that avoids recording secrets and sensitive data.
"""

import json
import logging
import os
import traceback
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


_LOGGER_NAME = "medislot"
logger = logging.getLogger(_LOGGER_NAME)
logger.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())

# Lambda automatically includes the log message and timestamp in CloudWatch.
# JSON formatting makes individual application events easier to search.
logger.propagate = True

_SENSITIVE_KEYS = {
    "password",
    "password_hash",
    "token",
    "access_token",
    "refresh_token",
    "authorization",
    "jwt",
    "secret",
    "secret_key",
    "db_password",
    "database_password",
    "email",
    "phone",
    "date_of_birth",
    "emergency_contact",
}


def _redact(value: Any, key: str = "") -> Any:
    """Redact sensitive values in dictionaries and nested structures."""
    normalized_key = key.lower().replace("-", "_")

    if any(
        sensitive in normalized_key
        for sensitive in _SENSITIVE_KEYS
    ):
        return "[REDACTED]"

    if isinstance(value, dict):
        return {
            str(item_key): _redact(item_value, str(item_key))
            for item_key, item_value in value.items()
        }

    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]

    if isinstance(value, (str, int, float, bool)) or value is None:
        return value

    return str(value)


def get_correlation_id(event: dict | None = None, context: Any = None) -> str:
    """
    Resolve a correlation ID from the incoming request.

    Priority:
    1. API Gateway x-correlation-id header.
    2. API Gateway request ID.
    3. Lambda invocation request ID.
    4. Newly generated ID.
    """
    event = event or {}

    headers = event.get("headers") or {}

    correlation_id = None

    if isinstance(headers, dict):
        for name, value in headers.items():
            if name.lower() == "x-correlation-id" and value:
                correlation_id = str(value)
                break

    request_context = event.get("requestContext") or {}

    if not correlation_id:
        correlation_id = request_context.get("requestId")

    if not correlation_id and context is not None:
        correlation_id = getattr(context, "aws_request_id", None)

    return str(correlation_id or uuid4())


def log_event(
    level: str,
    event_name: str,
    message: str,
    correlation_id: str | None = None,
    **fields: Any,
) -> None:
    """
    Write a structured JSON application event.

    Do not pass passwords, tokens, patient details, or other sensitive
    information as fields. Sensitive dictionary keys are redacted.
    """
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "service": "medislot",
        "event": event_name,
        "message": message,
        "correlation_id": correlation_id or "not_available",
        **_redact(fields),
    }

    serialized = json.dumps(record, separators=(",", ":"), default=str)

    log_level = getattr(logging, level.upper(), logging.INFO)
    logger.log(log_level, serialized)


def log_info(
    event_name: str,
    message: str,
    correlation_id: str | None = None,
    **fields: Any,
) -> None:
    """Log an informational event."""
    log_event(
        "INFO",
        event_name,
        message,
        correlation_id,
        **fields,
    )


def log_warning(
    event_name: str,
    message: str,
    correlation_id: str | None = None,
    **fields: Any,
) -> None:
    """Log a warning event."""
    log_event(
        "WARNING",
        event_name,
        message,
        correlation_id,
        **fields,
    )


def log_error(
    event_name: str,
    message: str,
    correlation_id: str | None = None,
    **fields: Any,
) -> None:
    """Log an error event."""
    log_event(
        "ERROR",
        event_name,
        message,
        correlation_id,
        **fields,
    )


def log_exception(
    event_name: str,
    message: str,
    correlation_id: str | None = None,
    **fields: Any,
) -> None:
    """
    Log an exception with its type and traceback.

    Call this function from inside an except block.
    Avoid including raw exception messages if they may contain
    credentials, SQL parameters, or personal information.
    """
    exception = __import__("sys").exc_info()[1]

    safe_fields = _redact(fields)

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "service": "medislot",
        "event": event_name,
        "message": message,
        "correlation_id": correlation_id or "not_available",
        **safe_fields,
    }

    if exception is not None:
        record["exception_type"] = type(exception).__name__

        # Do not log the raw exception message: it may contain sensitive data.
        record["traceback"] = traceback.format_exc()

    logger.error(
        json.dumps(record, separators=(",", ":"), default=str)
    )
