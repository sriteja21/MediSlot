"""Allow-listed structured logging utilities for MediSlot Lambdas."""

import inspect
import json
import logging
import os
import re
import sys
import traceback
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


_LOGGER_NAME = "medislot"
logger = logging.getLogger(_LOGGER_NAME)
_configured_level = os.getenv("LOG_LEVEL", "INFO").upper()
logger.setLevel(
    {"INFO": logging.INFO, "WARN": logging.WARNING, "ERROR": logging.ERROR}.get(
        _configured_level, logging.INFO
    )
)
logger.propagate = False
if not logger.handlers:
    json_handler = logging.StreamHandler(sys.stdout)
    json_handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(json_handler)
_correlation_id_context: ContextVar[str | None] = ContextVar(
    "medislot_correlation_id", default=None
)

_ALLOWED_FIELDS = {
    "duration_ms",
    "deactivated_count",
    "error_message",
    "error_type",
    "field_names",
    "filters_used",
    "http_status",
    "index_count",
    "column_count",
    "attempt_count",
    "method",
    "overlap_count",
    "page",
    "page_size",
    "pattern_count",
    "result_count",
    "role",
    "route",
    "skipped_count",
    "stack_trace",
    "updated_count",
    "unavailable_date_count",
    "invalid_timezone_count",
    "invalid_pattern_count",
    "user_id",
}
_SENSITIVE_KEY_PARTS = (
    "password",
    "token",
    "secret",
    "authorization",
    "email",
    "phone",
    "date_of_birth",
    "emergency_contact",
    "address",
    "notes",
    "patient_name",
    "license",
)
_JWT_PATTERN = re.compile(
    r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{8,}\b"
)
_EMAIL_PATTERN = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_PHONE_PATTERN = re.compile(r"(?<!\w)\+?[0-9][0-9\s()\-]{5,}[0-9](?!\w)")
_CREDENTIAL_PATTERN = re.compile(
    r"(?i)\b(password|token|secret|authorization)\s*[:=]\s*[^\s,;]+"
)
_CORRELATION_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def get_correlation_id(event: dict[str, Any] | None = None, context: Any = None) -> str:
    """Use a valid X-Correlation-ID, then AWS request IDs, then a UUID."""
    event = event if isinstance(event, dict) else {}
    headers = event.get("headers") or {}
    correlation_id = None

    if isinstance(headers, dict):
        for name, value in headers.items():
            if str(name).lower() == "x-correlation-id" and isinstance(value, str):
                candidate = value.strip()
                if _CORRELATION_PATTERN.fullmatch(candidate):
                    correlation_id = candidate
                break

    request_context = event.get("requestContext")
    if not isinstance(request_context, dict):
        request_context = {}
    authorizer_context = request_context.get("authorizer")
    if not isinstance(authorizer_context, dict):
        authorizer_context = {}
    candidate = (
        correlation_id
        or authorizer_context.get("correlationId")
        or request_context.get("requestId")
    )
    candidate = candidate or getattr(context, "aws_request_id", None)
    if _is_valid_correlation_id(candidate):
        return candidate
    return str(uuid4())


def _is_valid_correlation_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(_CORRELATION_PATTERN.fullmatch(value))
        and not _JWT_PATTERN.search(value)
        and not _PHONE_PATTERN.search(value)
    )


def set_correlation_id(correlation_id: str) -> Token[str | None]:
    """Set the correlation ID for logs emitted during this invocation."""
    return _correlation_id_context.set(correlation_id)


def reset_correlation_id(token: Token[str | None]) -> None:
    """Restore the previous invocation's correlation ID."""
    _correlation_id_context.reset(token)


def current_correlation_id() -> str | None:
    return _correlation_id_context.get()


def extract_field_names(message: str, candidates: set[str]) -> list[str]:
    """Return only known schema field names mentioned in a validation error."""
    normalized_message = message.lower()
    return sorted(
        field_name
        for field_name in candidates
        if re.search(
            rf"(?<![a-z0-9_]){re.escape(field_name.lower())}(?![a-z0-9_])",
            normalized_message,
        )
    )


def _redact(value: Any, key: str = "") -> Any:
    normalized_key = key.lower().replace("-", "_").replace(" ", "_").replace(".", "_")
    if any(part in normalized_key for part in _SENSITIVE_KEY_PARTS):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            str(item_key): _redact(item_value, str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        value = _JWT_PATTERN.sub("[REDACTED]", value)
        value = _EMAIL_PATTERN.sub("[REDACTED]", value)
        value = _PHONE_PATTERN.sub("[REDACTED]", value)
        return _CREDENTIAL_PATTERN.sub(r"\1=[REDACTED]", value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return "[UNSUPPORTED_VALUE]"


def log_event(
    level: str,
    event_name: str,
    message: str,
    correlation_id: str | None = None,
    **fields: Any,
) -> None:
    """Emit one JSON line, dropping every field outside the fixed allow-list."""
    normalized_level = level.upper()
    if normalized_level == "WARNING":
        normalized_level = "WARN"
    if normalized_level not in {"INFO", "WARN", "ERROR"}:
        normalized_level = "INFO"
    if not re.fullmatch(r"[a-z0-9_]{1,64}", event_name):
        event_name = "application_event"

    safe_fields = {
        name: _redact(value, name)
        for name, value in fields.items()
        if name in _ALLOWED_FIELDS
        and (name != "stack_trace" or normalized_level == "ERROR")
    }
    active_correlation_id = correlation_id or current_correlation_id()
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "level": normalized_level,
        "service": os.getenv("AWS_LAMBDA_FUNCTION_NAME", _LOGGER_NAME),
        "correlation_id": (
            active_correlation_id
            if _is_valid_correlation_id(active_correlation_id)
            else str(uuid4())
        ),
        "event": event_name,
        "message": _redact(message, "message"),
        **safe_fields,
    }
    python_level = {"INFO": logging.INFO, "WARN": logging.WARNING, "ERROR": logging.ERROR}[normalized_level]
    logger.log(python_level, json.dumps(record, separators=(",", ":"), ensure_ascii=True))


def log_info(event_name: str, message: str, correlation_id: str | None = None, **fields: Any) -> None:
    log_event("INFO", event_name, message, correlation_id, **fields)


def log_warning(event_name: str, message: str, correlation_id: str | None = None, **fields: Any) -> None:
    log_event("WARN", event_name, message, correlation_id, **fields)


def log_error(event_name: str, message: str, correlation_id: str | None = None, **fields: Any) -> None:
    log_event("ERROR", event_name, message, correlation_id, **fields)


def log_exception(event_name: str, message: str, correlation_id: str | None = None, **fields: Any) -> None:
    exception = sys.exc_info()[1]
    fields["error_type"] = type(exception).__name__ if exception else "Exception"
    fields["error_message"] = "Unexpected internal failure"
    fields["stack_trace"] = [
        f"{frame.name}:{frame.lineno}"
        for frame in traceback.extract_tb(exception.__traceback__)
    ] if exception and exception.__traceback__ else []
    log_event("ERROR", event_name, message, correlation_id, **fields)


class _SafeLogger:
    """Compatibility logger that never forwards legacy message arguments."""

    def __init__(self, scope: str) -> None:
        self._scope = re.sub(r"[^a-z0-9]+", "_", scope.lower()).strip("_") or _LOGGER_NAME

    def _emit(self, level: str) -> None:
        frame = inspect.currentframe()
        caller = frame.f_back.f_back if frame and frame.f_back else None
        function_name = caller.f_code.co_name if caller else "application"
        function_name = re.sub(r"[^a-zA-Z0-9]+", "_", function_name).strip("_").lower()
        event_name = f"{self._scope}_{function_name}_{level.lower()}"
        message = "Application event recorded"
        if level == "ERROR":
            log_exception(event_name, message)
        else:
            log_event(level, event_name, message)

    def info(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self._emit("INFO")

    def warning(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self._emit("WARN")

    def error(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self._emit("ERROR")

    def exception(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self._emit("ERROR")


def get_logger(scope: str) -> _SafeLogger:
    """Return a structured, privacy-safe replacement for legacy loggers."""
    return _SafeLogger(scope)