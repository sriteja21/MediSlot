"""Role guards shared by the Flask doctor and patient blueprints."""

from functools import wraps
from typing import Any, Callable

from flask import flash, jsonify, redirect, request, session, url_for


def _role_required(role: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorate(view: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(view)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            has_token = bool(request.cookies.get("access_token") or request.cookies.get("refresh_token"))
            current_role = session.get("role")
            if has_token and current_role == role:
                return view(*args, **kwargs)
            if has_token and current_role in {"doctor", "patient"}:
                flash("That page is not available for your account type.", "warning")
                return redirect(url_for("index"))
            next_path = request.full_path[:-1] if request.full_path.endswith("?") else request.full_path
            flash("Please sign in to continue.", "warning")
            return redirect(url_for("login", role=role, next=next_path))

        return wrapped

    return decorate


doctor_required = _role_required("doctor")
patient_required = _role_required("patient")


def patient_api_required(view: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        has_token = bool(request.cookies.get("access_token") or request.cookies.get("refresh_token"))
        if not has_token:
            return jsonify({"message": "Patient sign-in required."}), 401
        if session.get("role") != "patient":
            return jsonify({"message": "Patient role required."}), 403
        return view(*args, **kwargs)

    return wrapped