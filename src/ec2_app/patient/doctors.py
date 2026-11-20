"""Doctor search, public doctor details, and slot browser pages."""

from flask import Blueprint, jsonify, render_template, request

from api_client import api_call
from route_guards import patient_api_required, patient_required


doctors_bp = Blueprint("patient_doctors", __name__)


@doctors_bp.get("/patient/doctors")
@patient_required
def doctor_search_page():
    return render_template("patient/doctors.html", active_page="doctors")


@doctors_bp.get("/patient/doctors/<doctor_id>")
@patient_required
def doctor_profile_page(doctor_id: str):
    status, payload = api_call("GET", f"/doctors/{doctor_id}")
    back_url = request.args.get("from", "")
    if not back_url.startswith("/") or back_url.startswith("//") or "\\" in back_url:
        back_url = "/patient/doctors"
    if status == 404:
        return render_template("patient/doctor-not-found.html", back_url=back_url), 404
    if status != 200:
        return render_template(
            "patient/doctor_profile.html",
            active_page="doctors",
            doctor_id=doctor_id,
            back_url=back_url,
            initial_error=payload.get("message", "Could not load this doctor."),
        ), status
    return render_template(
        "patient/doctor_profile.html",
        active_page="doctors",
        doctor_id=doctor_id,
        back_url=back_url,
        initial_doctor=payload.get("doctor"),
    )


@doctors_bp.get("/api/doctors/options")
@patient_api_required
def doctor_options():
    status, payload = api_call("GET", "/doctors/options")
    return jsonify(payload), status


@doctors_bp.get("/api/doctors")
@patient_api_required
def doctor_search():
    status, payload = api_call("GET", "/doctors", params=request.args.to_dict(flat=True))
    return jsonify(payload), status


@doctors_bp.get("/api/doctors/<doctor_id>")
@patient_api_required
def doctor_details(doctor_id: str):
    status, payload = api_call("GET", f"/doctors/{doctor_id}")
    return jsonify(payload), status


@doctors_bp.get("/api/doctors/<doctor_id>/slots")
@patient_api_required
def doctor_slots(doctor_id: str):
    status, payload = api_call(
        "GET",
        f"/doctors/{doctor_id}/slots",
        params=request.args.to_dict(flat=True),
    )
    return jsonify(payload), status
