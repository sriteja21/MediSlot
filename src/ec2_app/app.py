"""
MediSlot EC2 Flask Application

Provides the Doctor Web Portal interface and proxies authenticated requests
to the MediSlot API Gateway. Access and refresh tokens are stored in secure
HttpOnly cookies with automatic token refresh on 401 responses.
"""

import logging
import os
from functools import wraps
from typing import Any

import requests
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

API_BASE_URL = os.environ.get(
    "API_BASE_URL",
    "http://localhost:3000",
).rstrip("/")


# ============================================================
# COOKIE & API CALL HELPERS
# ============================================================

def api_call(
    method: str,
    path: str,
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    """
    Proxy an HTTP call to the API Gateway using the Bearer token in cookies.

    If the API Gateway returns 401, attempts to refresh the access token using
    the refresh_token cookie, retries the request once upon success, and sets
    the new token in Flask's request context for subsequent cookie updating.
    """
    token = request.cookies.get("access_token")
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = f"{API_BASE_URL}{path}"

    try:
        resp = requests.request(
            method=method,
            url=url,
            headers=headers,
            json=json_body,
            params=params,
            timeout=15,
        )
        status_code = resp.status_code
        try:
            data = resp.json()
        except Exception:
            data = {"message": resp.text or f"HTTP {status_code}"}
    except requests.RequestException as exc:
        logger.warning("API connection failed. url=%s exc=%s", url, exc)
        return 503, {
            "message": "Unable to communicate with MediSlot API Gateway. Please try again shortly.",
            "detail": str(exc),
        }

    # Handle 401 Unauthorized by attempting a token refresh
    if status_code == 401:
        refresh_token = request.cookies.get("refresh_token")
        if refresh_token:
            logger.info("Attempting access token refresh after 401 response")
            try:
                refresh_resp = requests.post(
                    f"{API_BASE_URL}/auth/refresh",
                    json={"refresh_token": refresh_token},
                    timeout=10,
                )
                if refresh_resp.status_code == 200:
                    refresh_data = refresh_resp.json()
                    new_access_token = refresh_data.get("access_token")
                    if new_access_token:
                        # Store in g so @app.after_request writes the updated cookie
                        g.new_access_token = new_access_token
                        headers["Authorization"] = f"Bearer {new_access_token}"

                        # Retry the original request once
                        retry_resp = requests.request(
                            method=method,
                            url=url,
                            headers=headers,
                            json=json_body,
                            params=params,
                            timeout=15,
                        )
                        try:
                            retry_data = retry_resp.json()
                        except Exception:
                            retry_data = {"message": retry_resp.text or f"HTTP {retry_resp.status_code}"}
                        return retry_resp.status_code, retry_data
            except requests.RequestException as ref_exc:
                logger.warning("Refresh token attempt failed: %s", ref_exc)

    return status_code, data


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
    return response


def login_required(f):
    """Ensure a doctor session exists before serving a doctor page."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        access_token = request.cookies.get("access_token")
        refresh_token = request.cookies.get("refresh_token")
        if (not access_token and not refresh_token) or session.get("role") != "doctor":
            flash("Please sign in with a doctor account to continue.", "warning")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated_function


def patient_required(f):
    """Ensure a patient session exists before serving patient pages."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        access_token = request.cookies.get("access_token")
        refresh_token = request.cookies.get("refresh_token")
        if (not access_token and not refresh_token) or session.get("role") != "patient":
            flash("Please sign in with a patient account to continue.", "warning")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated_function


# ============================================================
# PUBLIC AUTHENTICATION ROUTES
# ============================================================

@app.route("/", methods=["GET"])
def index():
    """Send authenticated users to the dashboard for their role."""
    if session.get("role") == "doctor":
        return redirect(url_for("doctor_dashboard"))
    if session.get("role") == "patient":
        return redirect(url_for("patient_dashboard"))
    return redirect(url_for("login"))


@app.route("/healthz", methods=["GET"])
def health_check():
    return {"status": "ok"}, 200


@app.route("/login", methods=["GET", "POST"])
def login():
    """Render unified login or process credentials for either role."""
    if request.method == "GET":
        if session.get("role") in {"doctor", "patient"}:
            return redirect(url_for("index"))
        return render_template("login.html")

    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")

    if not email or not password:
        flash("Email and password are required.", "danger")
        return redirect(url_for("login"))

    status, res = api_call(
        "POST",
        "/auth/login",
        json_body={"email": email, "password": password},
    )

    if status == 200 and "access_token" in res:
        user = res.get("user") or {}
        role = user.get("role")
        if role not in {"doctor", "patient"}:
            flash("This account has an unsupported role.", "danger")
            return redirect(url_for("login"))

        access_token = res["access_token"]
        refresh_token = res.get("refresh_token")
        expires_in = int(res.get("expires_in", 900))
        refresh_expires_in = int(res.get("refresh_expires_in", 7 * 86400))

        session.clear()
        session["role"] = role
        session["user_id"] = user.get("user_id")
        resp = redirect(url_for("index"))
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
    return redirect(url_for("login"))


@app.route("/register", methods=["GET", "POST"])
def register():
    """Render registration page or process doctor account creation."""
    if request.method == "GET":
        status, options = api_call("GET", "/auth/registration-options")
        if status != 200:
            logger.warning("Could not load doctor registration options. status=%s", status)
            flash("Registration options are temporarily unavailable. Please try again.", "warning")

        return render_template(
            "register.html",
            specialisations=options.get("specialisations", []),
            hospitals=options.get("hospitals", []),
        )

    full_name = request.form.get("full_name", "").strip()
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    licence_number = request.form.get("licence_number", "").strip()
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
        return redirect(url_for("register"))

    if not specialisation_ids:
        flash("Select at least one specialisation.", "danger")
        return redirect(url_for("register"))

    payload = {
        "full_name": full_name,
        "email": email,
        "password": password,
        "licence_number": licence_number,
        "specialisation_id": specialisation_ids[0],
        "specialisation_ids": specialisation_ids,
        "hospital_id": hospital_id,
        "years_experience": years_experience,
        "qualifications": qualifications or None,
        "timezone": timezone_name,
    }

    status, res = api_call("POST", "/auth/doctors/register", json_body=payload)

    if status in (200, 201):
        flash("Your doctor account has been registered successfully! Please sign in.", "success")
        return redirect(url_for("login"))

    error_msg = res.get("message") or "Registration failed. Please verify your details and try again."
    flash(error_msg, "danger")
    return redirect(url_for("register"))


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


@app.route("/patient/dashboard", methods=["GET"])
@patient_required
def patient_dashboard():
    return render_template("patient/dashboard.html", active_page="dashboard")


@app.route("/patient/doctors", methods=["GET"])
@patient_required
def patient_doctors():
    return render_template("patient/doctors.html", active_page="doctors")


@app.route("/patient/doctors/<doctor_id>", methods=["GET"])
@patient_required
def patient_doctor_profile(doctor_id: str):
    return render_template(
        "patient/doctor_profile.html",
        active_page="doctors",
        doctor_id=doctor_id,
    )


@app.route("/patient/appointments", methods=["GET"])
@patient_required
def patient_appointments():
    return render_template("patient/appointments.html", active_page="appointments")


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
def doctor_profile_update():
    """Proxy doctor profile updates to PATCH /doctors/me."""
    data = request.get_json(silent=True) or request.form.to_dict()
    status, res = api_call("PATCH", "/doctors/me", json_body=data)
    return jsonify(res), status


@app.route("/doctor/consultation-types/create", methods=["POST"])
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
def delete_consultation_type(id: str):
    """Proxy deletion of consultation types to DELETE /doctors/me/consultation-types/<id>."""
    status, res = api_call("DELETE", f"/doctors/me/consultation-types/{id}")
    return jsonify(res), status


@app.route("/doctor/availability/create", methods=["POST"])
def create_availability():
    """Proxy creation of availability pattern to POST /doctors/me/availability."""
    data = request.get_json(silent=True) or request.form.to_dict()
    if "day_of_week" in data:
        data["day_of_week"] = int(data["day_of_week"])
    status, res = api_call("POST", "/doctors/me/availability", json_body=data)
    return jsonify(res), status


@app.route("/doctor/availability/<id>/delete", methods=["POST"])
def delete_availability(id: str):
    """Proxy deletion of availability pattern to DELETE /doctors/me/availability/<id>."""
    status, res = api_call("DELETE", f"/doctors/me/availability/{id}")
    return jsonify(res), status


@app.route("/doctor/availability/<id>/update", methods=["POST"])
def update_availability(id: str):
    data = request.get_json(silent=True) or request.form.to_dict()
    if "day_of_week" in data:
        data["day_of_week"] = int(data["day_of_week"])
    status, res = api_call("PATCH", f"/doctors/me/availability/{id}", json_body=data)
    return jsonify(res), status


@app.route("/doctor/blackouts/create", methods=["POST"])
def create_blackout():
    """Proxy creation of blackout date to POST /doctors/me/blackouts."""
    data = request.get_json(silent=True) or request.form.to_dict()
    status, res = api_call("POST", "/doctors/me/blackouts", json_body=data)
    return jsonify(res), status


@app.route("/doctor/blackouts/<id>/delete", methods=["POST"])
def delete_blackout(id: str):
    """Proxy deletion of blackout date to DELETE /doctors/me/blackouts/<id>."""
    status, res = api_call("DELETE", f"/doctors/me/blackouts/{id}")
    return jsonify(res), status


@app.route("/doctor/photo-upload-url", methods=["GET"])
def photo_upload_url():
    """Proxy request for presigned S3 upload URL for doctor profile photo."""
    status, res = api_call("GET", "/doctors/me/photo-upload-url", params=request.args)
    return jsonify(res), status


# ============================================================
# DATA API PROXY ENDPOINTS (Called asynchronously by JavaScript)
# ============================================================

@app.route("/api/doctor/profile", methods=["GET"])
def api_doctor_profile():
    """Proxy to GET /doctors/me."""
    status, res = api_call("GET", "/doctors/me")
    return jsonify(res), status


@app.route("/api/doctor/consultation-types", methods=["GET"])
def api_doctor_consultation_types():
    """Proxy to GET /doctors/me/consultation-types."""
    status, res = api_call("GET", "/doctors/me/consultation-types", params=request.args)
    return jsonify(res), status


@app.route("/api/doctor/availability", methods=["GET"])
def api_doctor_availability():
    """Proxy to GET /doctors/me/availability."""
    status, res = api_call("GET", "/doctors/me/availability")
    return jsonify(res), status


@app.route("/api/doctor/blackouts", methods=["GET"])
def api_doctor_blackouts():
    """Proxy to GET /doctors/me/blackouts."""
    status, res = api_call("GET", "/doctors/me/blackouts")
    return jsonify(res), status


@app.route("/api/doctor/schedule", methods=["GET"])
def api_doctor_schedule():
    """Proxy to GET /doctors/me/schedule?date=..."""
    status, res = api_call("GET", "/doctors/me/schedule", params=request.args)
    return jsonify(res), status


@app.route("/api/doctor/dashboard", methods=["GET"])
def api_doctor_dashboard():
    """Proxy to GET /doctors/me/dashboard."""
    status, res = api_call("GET", "/doctors/me/dashboard")
    return jsonify(res), status


@app.route("/api/patient/dashboard", methods=["GET"])
def api_patient_dashboard():
    if session.get("role") != "patient":
        return jsonify({"message": "Patient sign-in required."}), 403
    status, res = api_call("GET", "/patients/me/dashboard")
    return jsonify(res), status


@app.route("/api/patient/doctors", methods=["GET"])
def api_patient_search_doctors():
    if session.get("role") != "patient":
        return jsonify({"message": "Patient sign-in required."}), 403
    status, res = api_call("GET", "/patients/doctors/search", params=request.args)
    return jsonify(res), status


@app.route("/api/patient/doctors/<doctor_id>", methods=["GET"])
def api_patient_doctor_profile(doctor_id: str):
    if session.get("role") != "patient":
        return jsonify({"message": "Patient sign-in required."}), 403
    status, res = api_call("GET", f"/patients/doctors/{doctor_id}")
    return jsonify(res), status


@app.route("/api/patient/doctors/<doctor_id>/slots", methods=["GET"])
def api_patient_doctor_slots(doctor_id: str):
    if session.get("role") != "patient":
        return jsonify({"message": "Patient sign-in required."}), 403
    status, res = api_call(
        "GET", f"/patients/doctors/{doctor_id}/slots", params=request.args
    )
    return jsonify(res), status


@app.route("/api/patient/book-appointment", methods=["POST"])
def api_patient_book_appointment():
    if session.get("role") != "patient":
        return jsonify({"message": "Patient sign-in required."}), 403

    data = request.get_json(silent=True) or request.form.to_dict()
    status, res = api_call("POST", "/patients/me/appointments", json_body=data)
    return jsonify(res), status


# ============================================================
# ENTRYPOINT
# ============================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    debug_mode = os.environ.get("FLASK_DEBUG", "false").lower() in ("true", "1")
    app.run(host="0.0.0.0", port=port, debug=debug_mode)
