"""
MediSlot database initialization Lambda.

Purpose:
    Initialize the MediSlot MySQL schema through a CloudFormation
    custom resource.

Environment variables:
    DB_HOST
    DB_PORT
    DB_NAME
    DB_USERNAME
    DB_PASSWORD_PARAMETER_NAME

Required dependencies:
    PyMySQL==1.1.1
    cryptography
"""

import json
import logging
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import boto3
import pymysql
from botocore.exceptions import BotoCoreError, ClientError
from pymysql.connections import Connection


# ============================================================
# LOGGING
# ============================================================

logger = logging.getLogger()
logger.setLevel(logging.INFO)


# ============================================================
# ENVIRONMENT CONFIGURATION
# ============================================================

DB_HOST = os.environ.get("DB_HOST", "").strip()
DB_PORT = int(os.environ.get("DB_PORT", "3306"))
DB_NAME = os.environ.get("DB_NAME", "").strip()
DB_USERNAME = os.environ.get("DB_USERNAME", "").strip()

DB_PASSWORD_PARAMETER_NAME = os.environ.get(
    "DB_PASSWORD_PARAMETER_NAME", ""
).strip()

AWS_REGION = os.environ.get("AWS_REGION", "ap-south-1")

MAX_CONNECTION_ATTEMPTS = 12
INITIAL_RETRY_DELAY_SECONDS = 5
MAX_RETRY_DELAY_SECONDS = 30

CONNECTION_TIMEOUT_SECONDS = 10
READ_TIMEOUT_SECONDS = 30
WRITE_TIMEOUT_SECONDS = 30

EXPECTED_TABLES = {
    "user_identities",
    "specialisations",
    "hospitals",
    "doctors",
    "doctor_languages",
    "patients",
    "consultation_types",
    "availability_patterns",
    "blackout_dates",
    "appointments",
    "appointment_events",
    "refund_ledger",
}

# schema.sql is packaged alongside this Python file.
SCHEMA_FILE = Path(__file__).resolve().parent / "schema.sql"


# ============================================================
# AWS CLIENTS
# ============================================================

ssm_client = boto3.client(
    "ssm",
    region_name=AWS_REGION,
)


# ============================================================
# CONFIGURATION VALIDATION
# ============================================================

def validate_configuration() -> None:
    """
    Validate required environment variables and local schema file.

    Do not log secrets or their values.
    """

    required_variables = {
        "DB_HOST": DB_HOST,
        "DB_NAME": DB_NAME,
        "DB_USERNAME": DB_USERNAME,
        "DB_PASSWORD_PARAMETER_NAME": DB_PASSWORD_PARAMETER_NAME,
    }

    missing_variables = [
        name
        for name, value in required_variables.items()
        if not value
    ]

    if missing_variables:
        raise ValueError(
            "Missing required environment variables: "
            + ", ".join(missing_variables)
        )

    if DB_PORT < 1 or DB_PORT > 65535:
        raise ValueError("DB_PORT must be between 1 and 65535.")

    if not SCHEMA_FILE.is_file():
        raise FileNotFoundError(
            f"Database schema file was not found: {SCHEMA_FILE}"
        )

    logger.info(
        "Configuration validated. Database host=%s, port=%s, "
        "database=%s.",
        DB_HOST,
        DB_PORT,
        DB_NAME,
    )


# ============================================================
# DATABASE PASSWORD
# ============================================================

def get_database_password() -> str:
    """
    Retrieve the database password from SSM Parameter Store.

    The password is never written to application logs.
    """

    logger.info(
        "Retrieving database password from SSM Parameter Store."
    )

    try:
        response = ssm_client.get_parameter(
            Name=DB_PASSWORD_PARAMETER_NAME,
            WithDecryption=True,
        )

        password = response["Parameter"]["Value"]

        if not password:
            raise ValueError(
                "The database password parameter is empty."
            )

        logger.info(
            "Database password retrieved successfully."
        )

        return password

    except (ClientError, BotoCoreError):
        logger.exception(
            "Failed to retrieve the database password from SSM. "
            "Check the parameter name, IAM permissions, and "
            "KMS permissions if the parameter uses SecureString."
        )
        raise


# ============================================================
# DATABASE CONNECTION
# ============================================================

def connect_to_database(password: str) -> Connection:
    """
    Establish a connection to the MediSlot MySQL database.

    The RDS security group must permit traffic from this Lambda's
    security group on the configured database port.
    """

    logger.info(
        "Attempting database connection to host=%s, port=%s.",
        DB_HOST,
        DB_PORT,
    )

    connection = pymysql.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USERNAME,
        password=password,
        database=DB_NAME,
        charset="utf8mb4",
        connect_timeout=CONNECTION_TIMEOUT_SECONDS,
        read_timeout=READ_TIMEOUT_SECONDS,
        write_timeout=WRITE_TIMEOUT_SECONDS,
        autocommit=False,
    )

    logger.info("Database connection established successfully.")

    return connection


def connect_with_retry(password: str) -> Connection:
    """
    Retry database connections while RDS becomes available.

    Uses exponential backoff with a maximum delay.
    """

    last_error = None

    for attempt in range(1, MAX_CONNECTION_ATTEMPTS + 1):
        try:
            logger.info(
                "Database connection attempt %s of %s.",
                attempt,
                MAX_CONNECTION_ATTEMPTS,
            )

            return connect_to_database(password)

        except pymysql.MySQLError as exc:
            last_error = exc

            if attempt == MAX_CONNECTION_ATTEMPTS:
                logger.exception(
                    "Database connection failed after %s attempts.",
                    MAX_CONNECTION_ATTEMPTS,
                )
                break

            delay = min(
                INITIAL_RETRY_DELAY_SECONDS * (2 ** (attempt - 1)),
                MAX_RETRY_DELAY_SECONDS,
            )

            logger.warning(
                "Database connection attempt %s failed. "
                "Retrying in %s seconds. Error type=%s.",
                attempt,
                delay,
                type(exc).__name__,
            )

            time.sleep(delay)

    raise RuntimeError(
        "Unable to connect to the MediSlot database after "
        f"{MAX_CONNECTION_ATTEMPTS} attempts."
    ) from last_error


# ============================================================
# SQL SCHEMA LOADING
# ============================================================

def load_schema_statements() -> list[str]:
    """
    Read schema.sql and split it into SQL statements.

    This parser is suitable for the current schema.sql, which uses
    ordinary CREATE TABLE statements without stored procedures or
    semicolons embedded inside SQL string literals.
    """

    logger.info("Loading SQL schema from %s.", SCHEMA_FILE.name)

    schema_text = SCHEMA_FILE.read_text(encoding="utf-8")

    if not schema_text.strip():
        raise ValueError("The schema.sql file is empty.")

    statements = [
        statement.strip()
        for statement in schema_text.split(";")
        if statement.strip()
    ]

    if not statements:
        raise ValueError(
            "No executable SQL statements were found in schema.sql."
        )

    logger.info(
        "Loaded %s SQL statements from schema.sql.",
        len(statements),
    )

    return statements


# ============================================================
# DATABASE INITIALIZATION
# ============================================================

def execute_schema(
    connection: Connection,
    statements: list[str],
) -> None:
    """
    Execute all schema statements.

    CREATE TABLE IF NOT EXISTS statements allow the initializer
    to run again without recreating existing tables.

    MySQL DDL can cause implicit commits, so this operation should
    not be treated as one atomic transaction.
    """

    total_statements = len(statements)

    try:
        with connection.cursor() as cursor:
            for index, statement in enumerate(
                statements,
                start=1,
            ):
                # Log the statement number, not the full SQL.
                logger.info(
                    "Executing schema statement %s of %s.",
                    index,
                    total_statements,
                )

                cursor.execute(statement)

                logger.info(
                    "Schema statement %s executed successfully.",
                    index,
                )

        connection.commit()

        logger.info(
            "All %s schema statements executed successfully.",
            total_statements,
        )

    except Exception:
        logger.exception(
            "Schema execution failed. "
            "Some earlier DDL statements may already have committed."
        )

        try:
            connection.rollback()
        except Exception:
            logger.exception(
                "Database rollback attempt failed."
            )

        raise


# ============================================================
# SCHEMA VERIFICATION
# ============================================================

def verify_schema(connection: Connection) -> list[str]:
    """
    Verify that every expected table exists in the configured
    database.

    Raises an exception if any table is missing.
    """

    logger.info(
        "Verifying expected MediSlot database tables."
    )

    query = """
        SELECT TABLE_NAME
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = %s
          AND TABLE_TYPE = 'BASE TABLE'
    """

    with connection.cursor() as cursor:
        cursor.execute(query, (DB_NAME,))
        rows = cursor.fetchall()

    # PyMySQL returns tuples by default unless a DictCursor is used.
    # Handle both row formats so verification works with the current
    # connection configuration and with DictCursor if configured later.
    existing_tables = set()
    for row in rows:
        if isinstance(row, dict):
            table_name = row.get("TABLE_NAME")
        else:
            table_name = row[0] if row else None

        if table_name:
            existing_tables.add(table_name)

    missing_tables = sorted(
        EXPECTED_TABLES - existing_tables
    )

    if missing_tables:
        logger.error(
            "Schema verification failed. Missing tables: %s.",
            ", ".join(missing_tables),
        )

        raise RuntimeError(
            "Schema verification failed. Missing tables: "
            + ", ".join(missing_tables)
        )

    verified_tables = sorted(EXPECTED_TABLES)

    logger.info(
        "Schema verification successful. "
        "%s expected tables are present.",
        len(verified_tables),
    )

    return verified_tables


# ============================================================
# INITIALIZATION WORKFLOW
# ============================================================

def initialize_database() -> dict[str, Any]:
    """
    Complete database initialization.

    Steps:
        1. Validate configuration.
        2. Read the database password from SSM.
        3. Connect to RDS with retry.
        4. Load schema.sql.
        5. Execute SQL statements.
        6. Verify the expected tables.
        7. Close the database connection.
    """

    connection = None

    try:
        validate_configuration()

        password = get_database_password()

        connection = connect_with_retry(password)

        statements = load_schema_statements()

        execute_schema(
            connection=connection,
            statements=statements,
        )

        verified_tables = verify_schema(connection)

        result = {
            "status": "SUCCESS",
            "database": DB_NAME,
            "tables_created_or_verified": len(verified_tables),
            "verified_tables": verified_tables,
        }

        logger.info(
            "MediSlot database initialization completed successfully."
        )

        return result

    finally:
        if connection is not None:
            try:
                connection.close()

                logger.info(
                    "Database connection closed."
                )

            except Exception:
                logger.exception(
                    "Failed to close the database connection cleanly."
                )


# ============================================================
# CLOUDFORMATION CUSTOM RESOURCE RESPONSE
# ============================================================

def send_cloudformation_response(
    event: dict[str, Any],
    context: Any,
    status: str,
    reason: str,
    physical_resource_id: str,
    data: dict[str, Any] | None = None,
) -> None:
    """
    Send a response to CloudFormation's presigned ResponseURL.

    CloudFormation custom resources require a response even when
    initialization fails. If the response cannot be delivered,
    CloudFormation may eventually time out.
    """

    response_url = event.get("ResponseURL")

    if not response_url:
        logger.warning(
            "ResponseURL is missing. This invocation may not be "
            "a CloudFormation custom-resource request."
        )
        return

    response_body = {
        "Status": status,
        "Reason": (
            f"{reason}. Log stream: "
            f"{getattr(context, 'log_stream_name', 'unknown')}"
        ),
        "PhysicalResourceId": physical_resource_id,
        "StackId": event.get("StackId", ""),
        "RequestId": event.get("RequestId", ""),
        "LogicalResourceId": event.get("LogicalResourceId", ""),
        "NoEcho": True,
        "Data": data or {},
    }

    encoded_body = json.dumps(response_body).encode("utf-8")

    request = urllib.request.Request(
        url=response_url,
        data=encoded_body,
        method="PUT",
        headers={
            "content-type": "",
            "content-length": str(len(encoded_body)),
        },
    )

    logger.info(
        "Sending CloudFormation custom-resource response: %s.",
        status,
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=15,
        ) as response:
            logger.info(
                "CloudFormation response delivered. HTTP status=%s.",
                response.status,
            )

    except (
        urllib.error.URLError,
        TimeoutError,
        OSError,
    ):
        logger.exception(
            "Failed to send the CloudFormation custom-resource response. "
            "Check network access to the presigned ResponseURL."
        )
        raise


# ============================================================
# LAMBDA ENTRY POINT
# ============================================================

def lambda_handler(
    event: dict[str, Any] | None,
    context: Any,
) -> dict[str, Any]:
    """
    Initialize the MediSlot database.

    Manual invocation:
        An empty event ({}), or any event without RequestType, runs
        database initialization directly. No CloudFormation response
        is attempted, so this works with the Lambda console Test button.

    CloudFormation custom-resource invocation:
        Create / Update initializes and verifies the schema.
        Delete preserves the database and its application data.
        A response is sent only for actual custom-resource events.
    """

    if not isinstance(event, dict):
        event = {}

    # A direct Lambda test such as {} should initialize the database.
    # Only events explicitly carrying RequestType are handled as
    # CloudFormation custom-resource requests.
    is_cloudformation_event = "RequestType" in event

    if not is_cloudformation_event:
        logger.info(
            "Received manual database initializer invocation. "
            "Starting schema initialization."
        )
        try:
            result = initialize_database()
            logger.info(
                "Manual database initialization completed successfully."
            )
            return result
        except Exception as exc:
            logger.exception(
                "Manual MediSlot database initialization failed."
            )
            # Do not attempt to call send_cloudformation_response here.
            return {
                "status": "FAILED",
                "error_type": type(exc).__name__,
                "message": (
                    "Database initialization failed. Check the Lambda "
                    "CloudWatch logs for the detailed error."
                ),
            }

    request_type = str(event.get("RequestType", "Unknown"))
    logical_resource_id = event.get("LogicalResourceId", "")

    logger.info(
        "Received CloudFormation database initializer event. "
        "RequestType=%s, LogicalResourceId=%s.",
        request_type,
        logical_resource_id,
    )

    physical_resource_id = event.get(
        "PhysicalResourceId",
        f"MediSlotDatabaseInitializer-{DB_NAME or 'database'}",
    )

    if request_type == "Delete":
        logger.info(
            "Delete request received. Database tables and application "
            "data will not be deleted."
        )
        try:
            send_cloudformation_response(
                event=event,
                context=context,
                status="SUCCESS",
                reason=(
                    "Database initializer deleted; "
                    "database contents were preserved"
                ),
                physical_resource_id=physical_resource_id,
                data={"Message": "Database contents were preserved."},
            )
        except Exception:
            logger.exception(
                "Could not deliver the Delete response to CloudFormation."
            )
            raise

        return {
            "status": "SUCCESS",
            "request_type": "Delete",
            "database_preserved": True,
        }

    if request_type not in {"Create", "Update"}:
        error_message = (
            f"Unsupported CloudFormation RequestType: {request_type}"
        )
        logger.error(error_message)
        try:
            send_cloudformation_response(
                event=event,
                context=context,
                status="FAILED",
                reason=error_message,
                physical_resource_id=physical_resource_id,
                data={},
            )
        except Exception:
            logger.exception(
                "Failed to report the invalid custom-resource event "
                "to CloudFormation."
            )
        return {"status": "FAILED", "reason": error_message}

    try:
        result = initialize_database()
        send_cloudformation_response(
            event=event,
            context=context,
            status="SUCCESS",
            reason="MediSlot database schema initialized and verified",
            physical_resource_id=physical_resource_id,
            data={
                "Database": result["database"],
                "VerifiedTableCount": result["tables_created_or_verified"],
            },
        )
        return result

    except Exception as exc:
        logger.exception("MediSlot database initialization failed.")
        safe_error_message = (
            f"Database initialization failed: {type(exc).__name__}"
        )
        try:
            send_cloudformation_response(
                event=event,
                context=context,
                status="FAILED",
                reason=safe_error_message,
                physical_resource_id=physical_resource_id,
                data={},
            )
        except Exception:
            logger.exception(
                "Failed to report the initialization failure to "
                "CloudFormation. Check network access to ResponseURL."
            )
        return {
            "status": "FAILED",
            "error_type": type(exc).__name__,
            "message": safe_error_message,
        }
