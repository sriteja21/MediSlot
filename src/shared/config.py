"""
MediSlot shared configuration utilities.

Responsibilities:
- Read required environment variables.
- Retrieve parameters from AWS Systems Manager Parameter Store.
- Retrieve the JWT signing secret from SSM SecureString.
- Load configurable application settings.
- Cache parameters during the Lambda execution environment's lifetime.

Never log secrets or parameter values.
"""

import json
import os
from functools import lru_cache
from typing import Any

import boto3


_ssm_client = boto3.client("ssm")


def get_required_env(name: str) -> str:
    """Retrieve a required environment variable."""
    value = os.environ.get(name)

    if not value:
        raise RuntimeError(
            f"Required environment variable '{name}' is not configured."
        )

    return value


@lru_cache(maxsize=128)
def get_ssm_parameter(
    parameter_name: str,
    decrypt: bool = True,
) -> str:
    """
    Retrieve a parameter from SSM Parameter Store.

    SecureString parameters are decrypted when decrypt=True.
    Parameter values are cached for this Lambda execution environment.
    """
    response = _ssm_client.get_parameter(
        Name=parameter_name,
        WithDecryption=decrypt,
    )

    return response["Parameter"]["Value"]


@lru_cache(maxsize=1)
def get_application_config() -> dict[str, Any]:
    """
    Load application settings from an optional JSON SSM parameter.

    APP_CONFIG_PARAMETER_NAME must contain the full parameter name.
    The parameter value must be a JSON object.
    """
    parameter_name = os.environ.get("APP_CONFIG_PARAMETER_NAME")

    if not parameter_name:
        return {}

    raw_value = get_ssm_parameter(
        parameter_name=parameter_name,
        decrypt=True,
    )

    try:
        config = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "The application configuration parameter contains invalid JSON."
        ) from exc

    if not isinstance(config, dict):
        raise RuntimeError(
            "The application configuration parameter must contain a JSON object."
        )

    return config


def get_jwt_secret() -> str:
    """
    Retrieve the JWT signing secret from SSM SecureString.

    Required environment variable:
        JWT_SECRET_PARAMETER_NAME
    """
    parameter_name = get_required_env("JWT_SECRET_PARAMETER_NAME")

    return get_ssm_parameter(
        parameter_name=parameter_name,
        decrypt=True,
    )


def get_setting(
    setting_name: str,
    default: Any = None,
) -> Any:
    """Return an application setting or its default value."""
    return get_application_config().get(setting_name, default)
