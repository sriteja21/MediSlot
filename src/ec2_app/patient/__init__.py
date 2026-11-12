"""Patient portal route blueprints."""

from flask import Flask

from patient.doctors import doctors_bp
from patient.profile import profile_bp


def register_blueprints(app: Flask) -> None:
    app.register_blueprint(doctors_bp)
    app.register_blueprint(profile_bp)
