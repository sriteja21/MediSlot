import json
import logging

from shared.logging_utils import get_correlation_id, log_event


def test_log_event_drops_unknown_fields_and_redacts_values(caplog, monkeypatch):
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "MediSlot-PatientFunction-dev")
    jwt_value = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTYifQ.signaturevalue123"

    with caplog.at_level(logging.INFO, logger="medislot"):
        log_event(
            "INFO",
            "doctor_search_completed",
            "Doctor search completed",
            "corr-123",
            user_id="opaque-user-id",
            result_count=4,
            email="patient@example.com",
            arbitrary_field="must not be logged",
            error_message=f"failed with password=hunter2 token={jwt_value}",
        )

    record = json.loads(caplog.records[-1].message)
    serialized = json.dumps(record)
    assert record["service"] == "MediSlot-PatientFunction-dev"
    assert record["correlation_id"] == "corr-123"
    assert record["user_id"] == "opaque-user-id"
    assert record["result_count"] == 4
    assert "email" not in record
    assert "arbitrary_field" not in record
    assert "hunter2" not in serialized
    assert jwt_value not in serialized
    assert "[REDACTED]" in serialized


def test_log_event_redacts_sensitive_key_and_normalizes_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="medislot"):
        log_event(
            "WARNING",
            "patient_validation_failed",
            "Patient input validation failed",
            "corr-456",
            field_names=["phone", "date_of_birth"],
            refresh_token="never-log-this",
        )

    record = json.loads(caplog.records[-1].message)
    assert record["level"] == "WARN"
    assert record["field_names"] == ["phone", "date_of_birth"]
    assert "refresh_token" not in record


def test_get_correlation_id_uses_header_and_rejects_unsafe_values():
    assert get_correlation_id({"headers": {"x-correlation-id": "edge-123"}}) == "edge-123"
    assert get_correlation_id({"headers": {"X-Correlation-ID": "invalid id"}}) != "invalid id"