
"""
MediSlot Lambda Authorizer.

Responsibilities:
- Extract the Bearer JWT from the Authorization header.
- Verify JWT signature, expiry, claims, and role.
- Reject access tokens that have been revoked.
- Enforce doctor-only and patient-only routes.
- Return an IAM policy and trusted identity context.

Expected JWT claims:
    sub  - User UUID
    role - doctor or patient
    iat  - Issued-at timestamp
    exp  - Expiration timestamp
    jti  - Unique access-token identifier
"""

import json
import logging
import os
import time

import boto3
import jwt
from botocore.exceptions import BotoCoreError, ClientError

from shared.config import get_jwt_secret


# ---------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------

ENVIRONMENT = os.environ.get("ENVIRONMENT", "dev")

TOKEN_DENYLIST_TABLE_NAME = os.environ.get(
    "TOKEN_DENYLIST_TABLE_NAME",
    f"MediSlot-TokenDenylist-{ENVIRONMENT}",
)

ACCESS_TOKEN_MAX_LIFETIME_SECONDS = 15 * 60

ALLOWED_ROLES = {"doctor", "patient"}

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb")
denylist_table = dynamodb.Table(TOKEN_DENYLIST_TABLE_NAME)


# ---------------------------------------------------------
# LOGGING
# ---------------------------------------------------------

def log_event(level, event_name, **details):
    """
    Write structured JSON logs without recording JWTs,
    passwords, refresh tokens, or signing secrets.
    """
    record = {
        "event": event_name,
        **details,
    }

    getattr(logger, level)(
        json.dumps(record, default=str)
    )


# ---------------------------------------------------------
# IAM POLICY GENERATION
# ---------------------------------------------------------

def generate_policy(principal_id, effect, resource, context=None):
    """
    Generate the IAM policy returned to API Gateway.

    The policy resource is restricted to the current method ARN.
    This is suitable when authorizer caching is disabled.
    """
    statement = {
        "Action": "execute-api:Invoke",
        "Effect": effect,
        "Resource": resource,
    }

    policy = {
        "principalId": str(principal_id),
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [statement],
        },
    }

    if context and effect == "Allow":
        policy["context"] = {
            key: str(value)
            for key, value in context.items()
        }

    return policy


# ---------------------------------------------------------
# TOKEN EXTRACTION
# ---------------------------------------------------------

def extract_token(event):
    """
    Extract a Bearer token from a REST API TOKEN authorizer
    event, or from headers if a REQUEST event is supplied.
    """
    token_value = event.get("authorizationToken")

    if not token_value:
        headers = event.get("headers") or {}

        for key, value in headers.items():
            if key.lower() == "authorization":
                token_value = value
                break

    if not isinstance(token_value, str):
        raise ValueError("Missing authorization token")

    parts = token_value.strip().split()

    if len(parts) != 2:
        raise ValueError("Invalid authorization header")

    scheme, token = parts

    if scheme.lower() != "bearer" or not token:
        raise ValueError("Invalid authorization scheme")

    return token


# ---------------------------------------------------------
# JWT VALIDATION
# ---------------------------------------------------------

def validate_access_token(token):
    """
    Verify signature, expiry, required claims, role, and
    maximum permitted access-token lifetime.

    Raises an exception if the token is invalid.
    """
    secret = get_jwt_secret()

    claims = jwt.decode(
        token,
        secret,
        algorithms=["HS256"],
        options={
            "require": [
                "sub",
                "role",
                "iat",
                "exp",
                "jti",
            ],
            "verify_signature": True,
            "verify_exp": True,
            "verify_iat": True,
        },
    )

    expected_claims = {
        "sub",
        "role",
        "iat",
        "exp",
        "jti",
    }

    if set(claims.keys()) != expected_claims:
        raise ValueError("Unexpected JWT claims")

    user_id = claims.get("sub")
    role = claims.get("role")
    token_id = claims.get("jti")
    issued_at = claims.get("iat")
    expires_at = claims.get("exp")

    if not isinstance(user_id, str) or not user_id.strip():
        raise ValueError("Invalid subject claim")

    if role not in ALLOWED_ROLES:
        raise ValueError("Invalid role claim")

    if not isinstance(token_id, str) or not token_id.strip():
        raise ValueError("Invalid token identifier")

    if (
        isinstance(issued_at, bool)
        or not isinstance(issued_at, int)
    ):
        raise ValueError("Invalid issued-at claim")

    if (
        isinstance(expires_at, bool)
        or not isinstance(expires_at, int)
    ):
        raise ValueError("Invalid expiration claim")

    now = int(time.time())

    if issued_at > now:
        raise ValueError("Token issued in the future")

    if expires_at <= issued_at:
        raise ValueError("Invalid token lifetime")

    if expires_at - issued_at > ACCESS_TOKEN_MAX_LIFETIME_SECONDS:
        raise ValueError("Access-token lifetime exceeds policy")

    if expires_at <= now:
        raise ValueError("Access token has expired")

    return claims


# ---------------------------------------------------------
# TOKEN REVOCATION CHECK
# ---------------------------------------------------------

def is_token_revoked(token_id):
    """
    Check whether the token's jti is present in the DynamoDB
    denylist. A consistent read avoids relying on stale data.
    """
    result = denylist_table.get_item(
        Key={"jti": token_id},
        ConsistentRead=True,
    )

    return "Item" in result


# ---------------------------------------------------------
# ROUTE ROLE ENFORCEMENT
# ---------------------------------------------------------

def required_role_from_method_arn(method_arn):
    """
    Determine whether an API route requires a specific role.

    Supported route conventions:
        /doctor/...  -> doctor
        /doctors/... -> doctor
        /patient/... -> patient
        /patients/... -> patient

    Authentication endpoints are not assigned a role here.
    They should be configured as public routes where appropriate.
    """
    try:
        arn_parts = method_arn.split(":", 5)

        if len(arn_parts) != 6:
            raise ValueError("Invalid method ARN")

        resource_part = arn_parts[5]

        # Expected resource format:
        # api-id/stage/HTTP-METHOD/path/to/resource
        resource_parts = resource_part.split("/", 3)

        if len(resource_parts) < 3:
            raise ValueError("Invalid API Gateway resource")

        path = ""

        if len(resource_parts) == 4:
            path = resource_parts[3]

        path = "/" + path.lstrip("/")
        path = path.rstrip("/") or "/"

        segments = [
            segment.lower()
            for segment in path.split("/")
            if segment
        ]

        if not segments:
            return None

        first_segment = segments[0]

        if first_segment in {"doctor", "doctors"}:
            return "doctor"

        if first_segment in {"patient", "patients"}:
            return "patient"

        return None

    except (IndexError, ValueError):
        raise ValueError("Unable to determine route role")


# ---------------------------------------------------------
# LAMBDA HANDLER
# ---------------------------------------------------------

def lambda_handler(event, context):
    """
    API Gateway TOKEN authorizer entry point.

    Invalid, expired, tampered, or revoked tokens produce
    Unauthorized, which API Gateway handles as HTTP 401.

    A valid token with the wrong role receives a Deny policy,
    which API Gateway handles as HTTP 403.
    """
    request_id = getattr(context, "aws_request_id", "unknown")

    method_arn = event.get("methodArn")

    if not method_arn:
        log_event(
            "error",
            "authorizer_missing_method_arn",
            request_id=request_id,
        )
        raise Exception("Unauthorized")

    # Invalid credentials must become API Gateway Unauthorized (401).
    # Do not include the token or raw validation error in logs.
    try:
        token = extract_token(event)
        claims = validate_access_token(token)
    except (ValueError, jwt.InvalidTokenError, TypeError) as exc:
        log_event(
            "warning",
            "authorizer_access_rejected",
            request_id=request_id,
            error_type=type(exc).__name__,
        )
        raise Exception("Unauthorized") from None

    user_id = claims["sub"]
    role = claims["role"]
    token_id = claims["jti"]

    # A DynamoDB/SSM outage is an infrastructure failure, not proof
    # that the caller supplied an invalid token. Fail closed and log it.
    try:
        revoked = is_token_revoked(token_id)
    except (ClientError, BotoCoreError):
        log_event(
            "error",
            "authorizer_denylist_check_failed",
            request_id=request_id,
            user_id=user_id,
            role=role,
        )
        raise

    if revoked:
        log_event(
            "warning",
            "authorizer_revoked_token",
            request_id=request_id,
            user_id=user_id,
            role=role,
        )
        raise Exception("Unauthorized")

    try:
        required_role = required_role_from_method_arn(method_arn)
    except ValueError as exc:
        log_event(
            "error",
            "authorizer_invalid_method_arn",
            request_id=request_id,
            error_type=type(exc).__name__,
        )
        raise

    # A valid token belonging to the wrong role receives a Deny policy.
    if required_role and role != required_role:
        log_event(
            "warning",
            "authorizer_role_denied",
            request_id=request_id,
            user_id=user_id,
            actual_role=role,
            required_role=required_role,
        )
        return generate_policy(
            principal_id=user_id,
            effect="Deny",
            resource=method_arn,
        )

    log_event(
        "info",
        "authorizer_access_granted",
        request_id=request_id,
        user_id=user_id,
        role=role,
    )

    return generate_policy(
        principal_id=user_id,
        effect="Allow",
        resource=method_arn,
        context={
            "userId": user_id,
            "role": role,
            "jti": token_id,
        },
    )
