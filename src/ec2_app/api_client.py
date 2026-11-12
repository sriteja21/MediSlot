"""The only module that sends HTTP requests to MediSlot API Gateway."""

import logging
import os
import re
from typing import Any
from uuid import uuid4

import requests
from flask import g, request


logger = logging.getLogger("medislot_api_client")
API_BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:3000").rstrip("/")
API_TIMEOUT_SECONDS = int(os.environ.get("API_TIMEOUT_SECONDS", "15"))
_CORRELATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _json_response(response: requests.Response) -> dict[str, Any] | list[Any]:
    try:
        data = response.json()
    except ValueError:
        return {"message": "Unexpected API response."}
    return data if isinstance(data, (dict, list)) else {"message": "Unexpected API response."}


def api_call(
    method: str,
    path: str,
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any] | list[Any]]:
    """Call the API, refresh once on 401, and return a safe JSON result."""
    correlation_id = request.headers.get("X-Correlation-ID", "").strip()
    if not _CORRELATION_ID_PATTERN.fullmatch(correlation_id):
        correlation_id = str(uuid4())
    g.api_correlation_id = correlation_id
    headers = {
        "Content-Type": "application/json",
        "X-Correlation-ID": correlation_id,
    }
    token = request.cookies.get("access_token")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = f"{API_BASE_URL}{path}"
    try:
        response = requests.request(
            method=method,
            url=url,
            headers=headers,
            json=json_body,
            params=params,
            timeout=API_TIMEOUT_SECONDS,
        )
    except requests.RequestException:
        logger.warning("API request failed to connect. method=%s route=%s correlation_id=%s", method, path, correlation_id)
        return 503, {"message": "MediSlot is temporarily unavailable. Please retry shortly."}

    data = _json_response(response)
    if response.status_code == 401 and path != "/auth/refresh":
        refresh_token = request.cookies.get("refresh_token")
        if refresh_token:
            try:
                refresh_response = requests.post(
                    f"{API_BASE_URL}/auth/refresh",
                    json={"refresh_token": refresh_token},
                    headers={"Content-Type": "application/json", "X-Correlation-ID": correlation_id},
                    timeout=API_TIMEOUT_SECONDS,
                )
                refresh_data = _json_response(refresh_response)
                new_token = (
                    refresh_data.get("access_token")
                    if refresh_response.status_code == 200
                    and isinstance(refresh_data, dict)
                    else None
                )
                if new_token:
                    g.new_access_token = new_token
                    headers["Authorization"] = f"Bearer {new_token}"
                    try:
                        response = requests.request(
                            method=method,
                            url=url,
                            headers=headers,
                            json=json_body,
                            params=params,
                            timeout=API_TIMEOUT_SECONDS,
                        )
                        data = _json_response(response)
                    except requests.RequestException:
                        return 503, {"message": "MediSlot is temporarily unavailable. Please retry shortly."}
                else:
                    g.session_expired = True
            except requests.RequestException:
                g.session_expired = True
        else:
            g.session_expired = True

    status = response.status_code
    if status == 403:
        if not isinstance(data, dict):
            data = {}
        data["message"] = data.get("message") or "You do not have permission to perform this action."
    elif status >= 500:
        data = {
            "message": "The service is temporarily unavailable. Please retry shortly.",
            "correlation_id": correlation_id,
        }
    elif status == 404:
        if not isinstance(data, dict):
            data = {}
        data["message"] = data.get("message") or "The requested resource was not found."
    elif status == 409:
        if not isinstance(data, dict):
            data = {}
        data["message"] = data.get("message") or "This request conflicts with the current state."
    elif status == 400:
        if not isinstance(data, dict):
            data = {}
        data["message"] = data.get("message") or "Please correct the highlighted fields."
    return status, data
