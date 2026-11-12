"""Patient profile page and Flask proxies."""

from flask import Blueprint, flash, jsonify, render_template, request, session

from api_client import api_call
from route_guards import patient_api_required, patient_required


profile_bp = Blueprint("patient_profile", __name__)


@profile_bp.get("/patient/profile")
@patient_required
def profile_page():
    return render_template("patient/profile.html", active_page="profile")


@profile_bp.get("/api/patient/me")
@patient_api_required
def get_profile():
    status, payload = api_call("GET", "/patients/me")
    return jsonify(payload), status


@profile_bp.put("/api/patient/me")
@patient_api_required
def update_profile():
    payload = request.get_json(silent=True)
    status, result = api_call("PUT", "/patients/me", json_body=payload if isinstance(payload, dict) else {})
    return jsonify(result), status


@profile_bp.delete("/api/patient/me")
@patient_api_required
def delete_profile():
    status, result = api_call("DELETE", "/patients/me")
    response = jsonify(result)
    if status == 200:
        session.clear()
        flash("Your account has been deleted.", "success")
        response.delete_cookie("access_token")
        response.delete_cookie("refresh_token")
    return response, status
