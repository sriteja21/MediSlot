"""
MediSlot EC2 Flask Application

Provides the Doctor Web Portal interface and proxies authenticated requests
to the MediSlot API Gateway. Access and refresh tokens are stored in secure
HttpOnly cookies with automatic token refresh on 401 responses.
"""

import logging
import os
from typing import Any
from urllib.parse import urlsplit

from flask import (
    Flask,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix

from api_client import api_call
from patient import register_blueprints as register_patient_blueprints
from route_guards import doctor_required as login_required

# ============================================================
# APP CONFIGURATION
# ============================================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("medislot_ec2_app")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY")
if not app.secret_key:
    raise RuntimeError("SECRET_KEY must be configured for the Flask application.")
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "true").lower() in {"true", "1"}
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=COOKIE_SECURE,
)

@app.after_request
def update_cookies_after_refresh(response):
    """If a new access token was issued during a 401 refresh, update the cookie."""
    new_token = getattr(g, "new_access_token", None)
    if new_token:
        response.set_cookie(
            "access_token",
            new_token,
            httponly=True,
            samesite="Lax",
            secure=COOKIE_SECURE,
            max_age=15 * 60,
        )
    if (
        getattr(g, "session_expired", False)
        and request.endpoint != "login"
        and not request.path.startswith("/api/")
    ):
        query_string = request.query_string.decode("utf-8", errors="replace")
        next_path = f"{request.path}?{query_string}" if query_string else request.path
        response = redirect(
            url_for(
                "login",
                role=session.get("role", "patient"),
                expired="1",
                next=_safe_next_url(next_path),
            )
        )
    correlation_id = getattr(g, "api_correlation_id", None)
    if correlation_id:
        response.headers["X-Correlation-ID"] = correlation_id
    return response


# ============================================================
# PUBLIC AUTHENTICATION ROUTES
# ============================================================

@app.route("/", methods=["GET"])
def index():
    """Send authenticated users to the first page in their workspace."""
    if session.get("role") == "doctor":
        return redirect(url_for("doctor_dashboard"))
    if session.get("role") == "patient":
        return redirect(url_for("patient_doctors.doctor_search_page"))
    return redirect(url_for("login"))


def _safe_next_url(candidate: str | None) -> str | None:
    if not candidate or not candidate.startswith("/") or candidate.startswith("//"):
        return None
    parsed = urlsplit(candidate)
    if parsed.scheme or parsed.netloc or "\\" in candidate:
        return None
    return candidate


@app.route("/healthz", methods=["GET"])
def health_check():
    return {"status": "ok"}, 200


@app.route("/login", methods=["GET", "POST"])
def login():
    """Render the role-selecting login form or authenticate the selected role."""
    if request.method == "GET":
        expired = request.args.get("expired") == "1"
        if expired:
            session.clear()
        elif session.get("role") in {"doctor", "patient"}:
            return redirect(url_for("index"))
        selected_role = request.args.get("role", "patient").lower()
        if selected_role not in {"doctor", "patient"}:
            selected_role = "patient"
        if expired:
            flash("Session expired, please log in again.", "warning")
        response = app.make_response(render_template(
            "login.html",
            selected_role=selected_role,
            next_url=_safe_next_url(request.args.get("next")),
        ))
        if expired:
            response.delete_cookie("access_token")
            response.delete_cookie("refresh_token")
        return response

    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    selected_role = request.form.get("role", "patient").strip().lower()
    next_url = _safe_next_url(request.form.get("next"))

    if selected_role not in {"doctor", "patient"}:
        flash("Select whether you are signing in as a patient or doctor.", "danger")
        return redirect(url_for("login", next=next_url) if next_url else url_for("login"))

    if not email or not password:
        flash("Email and password are required.", "danger")
        return redirect(url_for("login", role=selected_role, next=next_url) if next_url else url_for("login", role=selected_role))

    status, res = api_call(
        "POST",
        f"/auth/{selected_role}s/login",
        json_body={"email": email, "password": password},
    )

    if status == 200 and "access_token" in res:
        user = res.get("user") or {}
        role = user.get("role")
        if role != selected_role:
            flash("This account has an unsupported role.", "danger")
            return redirect(url_for("login", role=selected_role))

        access_token = res["access_token"]
        refresh_token = res.get("refresh_token")
        expires_in = int(res.get("expires_in", 900))
        refresh_expires_in = int(res.get("refresh_expires_in", 7 * 86400))

        session.clear()
        session["role"] = role
        session["user_id"] = user.get("user_id")
        resp = redirect(next_url or url_for("index"))
        resp.set_cookie(
            "access_token",
            access_token,
            httponly=True,
            samesite="Lax",
            secure=COOKIE_SECURE,
            max_age=expires_in,
        )
        if refresh_token:
            resp.set_cookie(
                "refresh_token",
                refresh_token,
                httponly=True,
                samesite="Lax",
                secure=COOKIE_SECURE,
                max_age=refresh_expires_in,
            )
        flash("Welcome back! You have successfully signed in.", "success")
        return resp

    error_msg = res.get("message") or "Invalid email or password. Please try again."
    flash(error_msg, "danger")
    return redirect(url_for("login", role=selected_role, next=next_url) if next_url else url_for("login", role=selected_role))


@app.route("/register", methods=["GET", "POST"])
def register():
    """Render the role-selecting registration form or create the selected account."""
    status, options = 200, {"specialisations": [], "hospitals": []}
    if request.method == "GET":
        status, options = api_call("GET", "/auth/registration-options")
        if status != 200:
            logger.warning("Could not load doctor registration options. status=%s", status)
            flash("Doctor registration options are temporarily unavailable.", "warning")

        selected_role = request.args.get("role", "patient").lower()
        if selected_role not in {"doctor", "patient"}:
            selected_role = "patient"

        return render_template(
            "register.html",
            specialisations=options.get("specialisations", []),
            hospitals=options.get("hospitals", []),
            selected_role=selected_role,
        )

    selected_role = request.form.get("role", "patient").strip().lower()
    if selected_role not in {"doctor", "patient"}:
        flash("Select whether you are registering as a patient or doctor.", "danger")
        return redirect(url_for("register"))

    full_name = request.form.get("full_name", "").strip()
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")

    payload = {"full_name": full_name, "email": email, "password": password}
    if selected_role == "patient":
        payload.update(
            {
                "date_of_birth": request.form.get("date_of_birth", "").strip(),
                "gender": request.form.get("gender", "").strip() or None,
                "phone": request.form.get("phone", "").strip() or None,
                "emergency_contact_name": request.form.get("emergency_contact_name", "").strip() or None,
                "emergency_contact_phone": request.form.get("emergency_contact_phone", "").strip() or None,
            }
        )
    else:
        specialisation_ids_raw = request.form.getlist("specialisation_ids")
        hospital_id = request.form.get("hospital_id", "").strip()
        years_experience_raw = request.form.get("years_experience", "0").strip()
        qualifications = request.form.get("qualifications", "").strip()
        timezone_name = request.form.get("timezone", "Asia/Kolkata").strip() or "Asia/Kolkata"

        try:
            specialisation_ids = [int(value) for value in specialisation_ids_raw]
            years_experience = int(years_experience_raw)
        except ValueError:
            flash("Select valid specialisations and enter a valid experience value.", "danger")
            return redirect(url_for("register", role=selected_role))

        if not specialisation_ids:
            flash("Select at least one specialisation.", "danger")
            return redirect(url_for("register", role=selected_role))

        payload.update(
            {
                "licence_number": request.form.get("licence_number", "").strip(),
                "specialisation_id": specialisation_ids[0],
                "specialisation_ids": specialisation_ids,
                "hospital_id": hospital_id,
                "years_experience": years_experience,
                "qualifications": qualifications or None,
                "timezone": timezone_name,
            }
        )

    status, res = api_call(
        "POST",
        f"/auth/{selected_role}s/register",
        json_body=payload,
    )

    if status in (200, 201):
        flash(f"Your {selected_role} account has been registered. Please sign in.", "success")
        return redirect(url_for("login", role=selected_role))

    error_msg = res.get("message") or "Registration failed. Please verify your details and try again."
    flash(error_msg, "danger")
    return redirect(url_for("register", role=selected_role))


@app.route("/logout", methods=["POST"])
def logout():
    """Revoke active tokens and clear cookies."""
    refresh_token = request.cookies.get("refresh_token")
    if refresh_token:
        try:
            api_call("POST", "/auth/logout", json_body={"refresh_token": refresh_token})
        except Exception as exc:
            logger.warning("Logout notification to API Gateway failed: %s", exc)

    resp = redirect(url_for("login"))
    session.clear()
    resp.delete_cookie("access_token")
    resp.delete_cookie("refresh_token")
    flash("You have been signed out successfully.", "info")
    return resp


# ============================================================
# DOCTOR DASHBOARD PAGES (HTML)
# ============================================================

@app.route("/doctor/dashboard", methods=["GET"])
@login_required
def doctor_dashboard():
    """Serve the doctor main dashboard overview."""
    return render_template("doctor/dashboard.html", active_page="dashboard")


@app.route("/doctor/profile", methods=["GET"])
@login_required
def doctor_profile():
    """Serve the doctor profile view & edit page."""
    return render_template("doctor/profile.html", active_page="profile")


@app.route("/doctor/consultation-types", methods=["GET"])
@login_required
def doctor_consultation_types():
    """Serve the consultation types management page."""
    return render_template("doctor/consultation_types.html", active_page="consultation_types")


@app.route("/doctor/availability", methods=["GET"])
@login_required
def doctor_availability():
    """Serve the weekly availability patterns configuration page."""
    return render_template("doctor/availability.html", active_page="availability")


@app.route("/doctor/blackouts", methods=["GET"])
@login_required
def doctor_blackouts():
    """Serve the blackout dates management page."""
    return render_template("doctor/blackouts.html", active_page="blackouts")


@app.route("/doctor/schedule", methods=["GET"])
@login_required
def doctor_schedule():
    """Serve the daily schedule timeline page."""
    status, profile = api_call("GET", "/doctors/me")
    timezone_name = profile.get("timezone", "UTC") if status == 200 else "UTC"
    return render_template(
        "doctor/schedule.html",
        active_page="schedule",
        doctor_timezone=timezone_name,
    )


# ============================================================
# DOCTOR ACTION PROXY ENDPOINTS (JSON responses for UI interactions)
# ============================================================

@app.route("/doctor/profile/update", methods=["POST"])
@login_required
def doctor_profile_update():
    """Proxy doctor profile updates to PATCH /doctors/me."""
    data = request.get_json(silent=True) or request.form.to_dict()
    status, res = api_call("PATCH", "/doctors/me", json_body=data)
    return jsonify(res), status


@app.route("/doctor/consultation-types/create", methods=["POST"])
@login_required
def create_consultation_type():
    """Proxy creation of consultation types to POST /doctors/me/consultation-types."""
    data = request.get_json(silent=True) or request.form.to_dict()
    if "duration_minutes" in data:
        data["duration_minutes"] = int(data["duration_minutes"])
    if "capacity" in data:
        data["capacity"] = int(data["capacity"])
    if "fee_amount" in data:
        data["fee_amount"] = float(data["fee_amount"])
    status, res = api_call("POST", "/doctors/me/consultation-types", json_body=data)
    return jsonify(res), status


@app.route("/doctor/consultation-types/<id>/update", methods=["POST"])
@login_required
def update_consultation_type(id: str):
    """Proxy update of consultation types to PATCH /doctors/me/consultation-types/<id>."""
    data = request.get_json(silent=True) or request.form.to_dict()
    if "duration_minutes" in data and data["duration_minutes"]:
        data["duration_minutes"] = int(data["duration_minutes"])
    if "capacity" in data and data["capacity"]:
        data["capacity"] = int(data["capacity"])
    if "fee_amount" in data and data["fee_amount"]:
        data["fee_amount"] = float(data["fee_amount"])
    status, res = api_call("PATCH", f"/doctors/me/consultation-types/{id}", json_body=data)
    return jsonify(res), status


@app.route("/doctor/consultation-types/<id>/delete", methods=["POST"])
@login_required
def delete_consultation_type(id: str):
    """Proxy deletion of consultation types to DELETE /doctors/me/consultation-types/<id>."""
    status, res = api_call("DELETE", f"/doctors/me/consultation-types/{id}")
    return jsonify(res), status


@app.route("/doctor/availability/create", methods=["POST"])
@login_required
def create_availability():
    """Proxy creation of availability pattern to POST /doctors/me/availability."""
    data = request.get_json(silent=True) or request.form.to_dict()
    if "day_of_week" in data:
        data["day_of_week"] = int(data["day_of_week"])
    status, res = api_call("POST", "/doctors/me/availability", json_body=data)
    return jsonify(res), status


@app.route("/doctor/availability/<id>/delete", methods=["POST"])
@login_required
def delete_availability(id: str):
    """Proxy deletion of availability pattern to DELETE /doctors/me/availability/<id>."""
    status, res = api_call("DELETE", f"/doctors/me/availability/{id}")
    return jsonify(res), status


@app.route("/doctor/availability/<id>/update", methods=["POST"])
@login_required
def update_availability(id: str):
    data = request.get_json(silent=True) or request.form.to_dict()
    if "day_of_week" in data:
        data["day_of_week"] = int(data["day_of_week"])
    status, res = api_call("PATCH", f"/doctors/me/availability/{id}", json_body=data)
    return jsonify(res), status


@app.route("/doctor/blackouts/create", methods=["POST"])
@login_required
def create_blackout():
    """Proxy creation of blackout date to POST /doctors/me/blackouts."""
    data = request.get_json(silent=True) or request.form.to_dict()
    status, res = api_call("POST", "/doctors/me/blackouts", json_body=data)
    return jsonify(res), status


@app.route("/doctor/blackouts/<id>/delete", methods=["POST"])
@login_required
def delete_blackout(id: str):
    """Proxy deletion of blackout date to DELETE /doctors/me/blackouts/<id>."""
    status, res = api_call("DELETE", f"/doctors/me/blackouts/{id}")
    return jsonify(res), status


@app.route("/doctor/photo-upload-url", methods=["GET"])
@login_required
def photo_upload_url():
    """Proxy request for presigned S3 upload URL for doctor profile photo."""
    status, res = api_call("GET", "/doctors/me/photo-upload-url", params=request.args)
    return jsonify(res), status


# ============================================================
# DATA API PROXY ENDPOINTS (Called asynchronously by JavaScript)
# ============================================================

@app.route("/api/doctor/profile", methods=["GET"])
@login_required
def api_doctor_profile():
    """Proxy to GET /doctors/me."""
    status, res = api_call("GET", "/doctors/me")
    return jsonify(res), status


@app.route("/api/doctor/consultation-types", methods=["GET"])
@login_required
def api_doctor_consultation_types():
    """Proxy to GET /doctors/me/consultation-types."""
    status, res = api_call("GET", "/doctors/me/consultation-types", params=request.args)
    return jsonify(res), status


@app.route("/api/doctor/availability", methods=["GET"])
@login_required
def api_doctor_availability():
    """Proxy to GET /doctors/me/availability."""
    status, res = api_call("GET", "/doctors/me/availability")
    return jsonify(res), status


@app.route("/api/doctor/blackouts", methods=["GET"])
@login_required
def api_doctor_blackouts():
    """Proxy to GET /doctors/me/blackouts."""
    status, res = api_call("GET", "/doctors/me/blackouts")
    return jsonify(res), status


@app.route("/api/doctor/schedule", methods=["GET"])
@login_required
def api_doctor_schedule():
    """Proxy to GET /doctors/me/schedule?date=..."""
    status, res = api_call("GET", "/doctors/me/schedule", params=request.args)
    return jsonify(res), status


@app.route("/api/doctor/dashboard", methods=["GET"])
@login_required
def api_doctor_dashboard():
    """Proxy to GET /doctors/me/dashboard."""
    status, res = api_call("GET", "/doctors/me/dashboard")
    return jsonify(res), status


register_patient_blueprints(app)


# ============================================================
# ENTRYPOINT
# ============================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    debug_mode = os.environ.get("FLASK_DEBUG", "false").lower() in ("true", "1")
    app.run(host="0.0.0.0", port=port, debug=debug_mode)
