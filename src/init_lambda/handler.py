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
    bcrypt==4.2.1
    cryptography
"""

import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import bcrypt
import boto3
import pymysql
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from pymysql.connections import Connection

# The initializer package is deployed from src/ so the shared logger is available.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from shared.logging_utils import (
    current_correlation_id,
    get_correlation_id,
    get_logger,
    log_error,
    log_exception,
    log_info,
    reset_correlation_id,
    set_correlation_id,
)


# ============================================================
# LOGGING
# ============================================================

logger = get_logger("database_initializer")


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
ENVIRONMENT = os.environ.get("ENVIRONMENT", "dev").strip().lower()
SLOT_MATERIALISER_FUNCTION_NAME = os.environ.get(
    "SLOT_MATERIALISER_FUNCTION_NAME", ""
).strip()

MAX_CONNECTION_ATTEMPTS = 12
INITIAL_RETRY_DELAY_SECONDS = 5
MAX_RETRY_DELAY_SECONDS = 30

CONNECTION_TIMEOUT_SECONDS = 10
READ_TIMEOUT_SECONDS = 30
WRITE_TIMEOUT_SECONDS = 30

EXPECTED_TABLES = {
    "users",
    "patients",
    "hospitals",
    "doctors",
    "specializations",
    "doctor_specializations",
    "consultation_catalog",
    "consultation_types",
    "availability_patterns",
    "blackout_dates",
    "appointments",
    "payments",
    "refunds",
}

EXPECTED_COLUMNS = {
    "users": {"user_id", "email", "password_hash", "role", "is_active", "last_login_at"},
    "patients": {"patient_id", "user_id", "first_name", "last_name", "date_of_birth", "gender", "city", "notes"},
    "hospitals": {"hospital_id", "name", "address", "city"},
    "doctors": {"doctor_id", "user_id", "hospital_id", "first_name", "last_name", "license_number", "experience_years", "timezone"},
    "specializations": {"specialization_id", "name"},
    "doctor_specializations": {"doctor_id", "specialization_id"},
    "consultation_catalog": {"consultation_type_id", "name", "mode", "duration_minutes"},
    "consultation_types": {"consultation_type_id", "doctor_id", "catalog_type_id", "fee", "capacity", "is_active"},
    "availability_patterns": {"pattern_id", "consultation_type_id", "day_of_week", "start_time", "end_time"},
    "blackout_dates": {"blackout_id", "doctor_id", "start_datetime", "end_datetime"},
    "appointments": {"appointment_id", "patient_id", "consultation_type_id", "appointment_datetime", "appointment_status"},
    "payments": {"payment_id", "appointment_id", "amount", "payment_status", "payment_method"},
    "refunds": {"refund_id", "payment_id", "refund_amount", "refund_status"},
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
lambda_client = boto3.client(
    "lambda",
    region_name=AWS_REGION,
    config=Config(
        connect_timeout=CONNECTION_TIMEOUT_SECONDS,
        read_timeout=900,
        retries={"max_attempts": 1},
    ),
)

SAMPLE_HOSPITAL_IDS = (
    "a0000000-0000-0000-0000-000000000001",
    "a0000000-0000-0000-0000-000000000002",
    "a0000000-0000-0000-0000-000000000003",
)

SAMPLE_DOCTORS = (
    ("Aarav", "Sample", "Cardiology", 0, 7, 650.00),
    ("Ananya", "Sample", "Dermatology", 1, 5, 550.00),
    ("Arjun", "Sample", "General Medicine", 2, 12, 500.00),
    ("Diya", "Sample", "Endocrinology", 0, 9, 600.00),
    ("Ishaan", "Sample", "Neurology", 1, 15, 800.00),
    ("Kavya", "Sample", "Obstetrics and Gynaecology", 2, 8, 650.00),
    ("Meera", "Sample", "Paediatrics", 0, 6, 550.00),
    ("Neel", "Sample", "Psychiatry", 1, 11, 700.00),
    ("Priya", "Sample", "Orthopaedics", 2, 14, 750.00),
    ("Rohan", "Sample", "Pulmonology", 0, 10, 650.00),
    ("Sana", "Sample", "Cardiology", 1, 4, 600.00),
    ("Vihaan", "Sample", "Dermatology", 2, 18, 850.00),
    ("Zoya", "Sample", "General Medicine", 0, 3, 500.00),
    ("Kabir", "Sample", "Neurology", 1, 16, 800.00),
    ("Nisha", "Sample", "Paediatrics", 2, 13, 700.00),
)
DEMO_DOCTOR_PASSWORD = "Demo@12345"


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

    if ENVIRONMENT not in {"dev", "test", "prod"}:
        raise ValueError("ENVIRONMENT must be dev, test, or prod.")

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
            for statement in statements:
                cursor.execute(statement)

        connection.commit()

        log_info(
            "database_schema_statements_completed",
            "Database schema statements completed",
            result_count=total_statements,
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


def seed_sample_doctors(connection: Connection) -> int:
    """Idempotently create fictional, searchable doctors and weekly schedules."""
    specialization_ids: dict[str, int] = {}
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT specialization_id, name FROM specializations"
        )
        for specialization_id, name in cursor.fetchall():
            specialization_ids[str(name)] = int(specialization_id)

        cursor.execute(
            "SELECT consultation_type_id FROM consultation_catalog "
            "WHERE consultation_type_id = %s",
            ("STANDARD",),
        )
        if not cursor.fetchone():
            raise RuntimeError(
                "The STANDARD consultation catalog entry is missing."
            )

        try:
            for index, sample in enumerate(SAMPLE_DOCTORS, start=1):
                first_name, last_name, specialization, hospital_index, experience, fee = sample
                doctor_id = str(
                    uuid5(NAMESPACE_URL, f"medislot-demo-doctor-{index:02d}")
                )
                user_id = str(
                    uuid5(NAMESPACE_URL, f"medislot-demo-user-{index:02d}")
                )
                consultation_type_id = str(
                    uuid5(
                        NAMESPACE_URL,
                        f"medislot-demo-consultation-{index:02d}",
                    )
                )
                email = f"doctor{index:02d}@sample.medislot.example"
                cursor.execute(
                    "SELECT license_number FROM doctors WHERE doctor_id = %s",
                    (doctor_id,),
                )
                existing_doctor_license = cursor.fetchone()
                license_number = (
                    existing_doctor_license[0]
                    if existing_doctor_license
                    and re.fullmatch(r"MCI-\d{5}", existing_doctor_license[0])
                    else _new_sample_license(cursor, doctor_id)
                )

                if specialization not in specialization_ids:
                    raise RuntimeError(
                        "A sample doctor specialization is missing from the catalog."
                    )

                cursor.execute(
                    """
                    SELECT user_id, role, is_active
                    FROM users WHERE email = %s
                    """,
                    (email,),
                )
                existing_user = cursor.fetchone()
                if existing_user and (
                    existing_user[0] != user_id
                    or existing_user[1] != "DOCTOR"
                    or not existing_user[2]
                ):
                    raise RuntimeError(
                        "A sample doctor email conflicts with an existing account."
                    )

                password_hash = bcrypt.hashpw(
                    DEMO_DOCTOR_PASSWORD.encode("utf-8"),
                    bcrypt.gensalt(rounds=12),
                ).decode("utf-8")
                if not existing_user:
                    cursor.execute(
                        """
                        INSERT IGNORE INTO users (
                            user_id, email, password_hash, role, is_active
                        ) VALUES (%s, %s, %s, 'DOCTOR', TRUE)
                        """,
                        (user_id, email, password_hash),
                    )
                    cursor.execute(
                        "SELECT user_id, role, is_active FROM users WHERE email = %s",
                        (email,),
                    )
                    existing_user = cursor.fetchone()
                    if not existing_user or (
                        existing_user[0] != user_id
                        or existing_user[1] != "DOCTOR"
                        or not existing_user[2]
                    ):
                        raise RuntimeError(
                            "A sample doctor account could not be created safely."
                        )
                cursor.execute(
                    "UPDATE users SET password_hash = %s WHERE user_id = %s",
                    (password_hash, user_id),
                )

                cursor.execute(
                    """
                    INSERT IGNORE INTO doctors (
                        doctor_id, user_id, hospital_id, first_name,
                        last_name, license_number, experience_years,
                        qualification, timezone, languages, is_verified
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s,
                        %s, 'Asia/Kolkata', %s, TRUE
                    )
                    """,
                    (
                        doctor_id,
                        user_id,
                        SAMPLE_HOSPITAL_IDS[hospital_index],
                        first_name,
                        last_name,
                        license_number,
                        experience,
                        "Demo profile - fictional sample data",
                        "English, Kannada, Hindi",
                    ),
                )
                cursor.execute(
                    "SELECT user_id FROM doctors WHERE doctor_id = %s",
                    (doctor_id,),
                )
                existing_doctor = cursor.fetchone()
                if not existing_doctor or existing_doctor[0] != user_id:
                    raise RuntimeError(
                        "A sample doctor profile conflicts with existing data."
                    )
                cursor.execute(
                    "SELECT license_number FROM doctors WHERE doctor_id = %s",
                    (doctor_id,),
                )
                current_license = cursor.fetchone()
                if not current_license:
                    raise RuntimeError(
                        "A sample doctor license could not be verified."
                    )
                if not re.fullmatch(r"MCI-\d{5}", current_license[0]):
                    license_number = _new_sample_license(cursor, doctor_id)
                    cursor.execute(
                        "UPDATE doctors SET license_number = %s WHERE doctor_id = %s",
                        (license_number, doctor_id),
                    )

                cursor.execute(
                    """
                    INSERT IGNORE INTO doctor_specializations (
                        doctor_id, specialization_id
                    ) VALUES (%s, %s)
                    """,
                    (doctor_id, specialization_ids[specialization]),
                )
                cursor.execute(
                    """
                    INSERT IGNORE INTO consultation_types (
                        consultation_type_id, doctor_id, catalog_type_id,
                        fee, capacity, is_active
                    ) VALUES (%s, %s, 'STANDARD', %s, 1, TRUE)
                    """,
                    (consultation_type_id, doctor_id, fee),
                )
                cursor.execute(
                    """
                    SELECT doctor_id, catalog_type_id
                    FROM consultation_types
                    WHERE consultation_type_id = %s
                    """,
                    (consultation_type_id,),
                )
                existing_consultation = cursor.fetchone()
                if not existing_consultation or existing_consultation != (
                    doctor_id,
                    "STANDARD",
                ):
                    raise RuntimeError(
                        "A sample doctor consultation type conflicts with existing data."
                    )

                start_time, end_time = (
                    ("09:00:00", "12:00:00")
                    if index % 2
                    else ("14:00:00", "17:00:00")
                )
                weekdays = sorted(
                    {((index + offset - 1) % 5) + 1 for offset in range(3)}
                )
                for day_of_week in weekdays:
                    pattern_id = str(
                        uuid5(
                            NAMESPACE_URL,
                            "medislot-demo-availability-"
                            f"{index:02d}-{day_of_week}",
                        )
                    )
                    cursor.execute(
                        """
                        INSERT IGNORE INTO availability_patterns (
                            pattern_id, consultation_type_id, day_of_week,
                            start_time, end_time, is_active
                        ) VALUES (%s, %s, %s, %s, %s, TRUE)
                        """,
                        (
                            pattern_id,
                            consultation_type_id,
                            day_of_week,
                            start_time,
                            end_time,
                        ),
                    )

            connection.commit()
        except Exception:
            connection.rollback()
            raise

    log_info(
        "sample_doctors_seeded",
        "Fictional sample doctors and availability were ensured",
        result_count=len(SAMPLE_DOCTORS),
    )
    return len(SAMPLE_DOCTORS)


def _new_sample_license(cursor: Any, doctor_id: str) -> str:
    """Generate an unused five-digit MCI-shaped identifier for demo data."""
    for _ in range(100):
        license_number = f"MCI-{secrets.randbelow(100000):05d}"
        cursor.execute(
            "SELECT doctor_id FROM doctors WHERE license_number = %s",
            (license_number,),
        )
        owner = cursor.fetchone()
        if not owner or owner[0] == doctor_id:
            return license_number
    raise RuntimeError("Could not allocate a unique sample doctor license.")


def invoke_slot_materialiser() -> dict[str, Any]:
    """Generate DynamoDB slots from the seeded availability patterns."""
    if not SLOT_MATERIALISER_FUNCTION_NAME:
        raise ValueError(
            "SLOT_MATERIALISER_FUNCTION_NAME is not configured."
        )

    correlation_id = current_correlation_id()
    payload: dict[str, Any] = {}
    if correlation_id:
        payload["headers"] = {"X-Correlation-ID": correlation_id}

    response = lambda_client.invoke(
        FunctionName=SLOT_MATERIALISER_FUNCTION_NAME,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode("utf-8"),
    )
    result_payload = response["Payload"].read()
    result = json.loads(result_payload or b"{}")
    if (
        response.get("FunctionError")
        or not isinstance(result, dict)
        or result.get("statusCode") != 200
    ):
        raise RuntimeError("Slot materialisation did not complete successfully.")

    materialisation = result.get("body")
    if not isinstance(materialisation, dict):
        materialisation = {}
    log_info(
        "sample_doctor_slots_materialised",
        "Sample doctor availability was materialised into slots",
        result_count=materialisation.get("total_generated"),
    )
    return materialisation


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

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT TABLE_NAME, COLUMN_NAME
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = %s
            """,
            (DB_NAME,),
        )
        column_rows = cursor.fetchall()

    existing_columns: dict[str, set[str]] = {}
    for row in column_rows:
        if isinstance(row, dict):
            table_name = row.get("TABLE_NAME")
            column_name = row.get("COLUMN_NAME")
        else:
            table_name, column_name = row[0], row[1]
        if table_name and column_name:
            existing_columns.setdefault(table_name, set()).add(column_name)

    missing_columns = sorted(
        f"{table}.{column}"
        for table, columns in EXPECTED_COLUMNS.items()
        for column in columns - existing_columns.get(table, set())
    )
    if missing_columns:
        raise RuntimeError(
            "Schema verification failed. Existing tables need migration; "
            "missing columns: " + ", ".join(missing_columns)
        )

    verified_tables = sorted(EXPECTED_TABLES)

    logger.info(
        "Schema verification successful. "
        "%s expected tables are present.",
        len(verified_tables),
    )

    return verified_tables


def apply_schema_migrations(connection: Connection) -> None:
    """Add milestone columns and indexes to databases created by earlier schemas."""
    column_migrations = {
        "patients": {
            "city": "VARCHAR(100) NOT NULL DEFAULT ''",
            "notes": "TEXT NULL",
        },
        "hospitals": {
            "city": "VARCHAR(100) NOT NULL DEFAULT ''",
        },
    }
    index_migrations = {
        "hospitals": {
            "idx_hospitals_city_id": "(city, hospital_id)",
        },
        "doctors": {
            "idx_doctors_name": "(last_name, first_name)",
            "idx_doctors_hospital": "(hospital_id)",
            "idx_doctors_verified_experience": "(is_verified, experience_years, doctor_id)",
        },
        "doctor_specializations": {
            "idx_doctor_specializations_specialization": "(specialization_id, doctor_id)",
        },
        "consultation_types": {
            "idx_consultation_types_doctor_active": "(doctor_id, is_active)",
            "idx_consultation_types_active_fee": "(is_active, fee, doctor_id)",
        },
    }

    added_columns = 0
    added_indexes = 0
    with connection.cursor() as cursor:
        for table_name, columns in column_migrations.items():
            cursor.execute(
                """
                SELECT COLUMN_NAME
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                """,
                (DB_NAME, table_name),
            )
            existing_columns = {
                row["COLUMN_NAME"] if isinstance(row, dict) else row[0]
                for row in cursor.fetchall()
            }
            for column_name, definition in columns.items():
                if column_name not in existing_columns:
                    cursor.execute(
                        f"ALTER TABLE `{table_name}` ADD COLUMN `{column_name}` {definition}"
                    )
                    added_columns += 1

        for table_name, indexes in index_migrations.items():
            cursor.execute(
                """
                SELECT INDEX_NAME
                FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                """,
                (DB_NAME, table_name),
            )
            existing_indexes = {
                row["INDEX_NAME"] if isinstance(row, dict) else row[0]
                for row in cursor.fetchall()
            }
            for index_name, columns_sql in indexes.items():
                if index_name not in existing_indexes:
                    cursor.execute(
                        f"CREATE INDEX `{index_name}` ON `{table_name}` {columns_sql}"
                    )
                    added_indexes += 1

        cursor.execute(
            "UPDATE hospitals SET city = 'Bengaluru' "
            "WHERE city = '' AND address LIKE %s",
            ("%Bengaluru%",),
        )
        cursor.execute(
            "UPDATE hospitals SET city = 'Mysuru' "
            "WHERE city = '' AND address LIKE %s",
            ("%Mysuru%",),
        )

    connection.commit()
    log_info(
        "database_schema_migrations_completed",
        "Database schema migrations completed",
        column_count=added_columns,
        index_count=added_indexes,
    )


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

        apply_schema_migrations(connection)

        if ENVIRONMENT in {"dev", "test"}:
            seed_sample_doctors(connection)
            invoke_slot_materialiser()
        else:
            log_info(
                "sample_doctors_seed_skipped",
                "Sample doctor seeding is disabled in production",
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
        "Reason": reason,
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

def _handle_initializer(
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
                "message": "Database initialization failed.",
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
        error_message = "Unsupported CloudFormation request type."
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
        safe_error_message = "Database initialization failed."
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
            "message": "Database initialization failed.",
        }


def lambda_handler(
    event: dict[str, Any] | None,
    context: Any = None,
) -> dict[str, Any]:
    """Log the initializer invocation and keep unexpected failures generic."""
    event = event if isinstance(event, dict) else {}
    correlation_id = get_correlation_id(event, context)
    token = set_correlation_id(correlation_id)
    started = time.perf_counter()
    request_type_value = str(event.get("RequestType") or "INVOKE")
    request_type = (
        request_type_value
        if request_type_value in {"Create", "Update", "Delete", "INVOKE"}
        else "UNKNOWN"
    )
    log_info(
        "database_initializer_started",
        "Database initializer request started",
        correlation_id,
        method=request_type,
        route="database_initializer",
    )
    try:
        result = _handle_initializer(event, context)
    except Exception as exc:
        log_exception(
            "database_initializer_failed",
            "Database initialization failed unexpectedly",
            correlation_id,
            error_type=type(exc).__name__,
            error_message="Database initialization failed",
        )
        result = {
            "status": "FAILED",
            "message": "Database initialization failed.",
        }

    failed = isinstance(result, dict) and result.get("status") == "FAILED"
    duration_ms = int((time.perf_counter() - started) * 1000)
    if failed:
        if request_type == "UNKNOWN":
            log_warning(
                "database_initializer_rejected",
                "Database initializer request was rejected",
                correlation_id,
                http_status=400,
                duration_ms=duration_ms,
            )
        else:
            log_error(
                "database_initializer_failed",
                "Database initializer failed",
                correlation_id,
                http_status=500,
                duration_ms=duration_ms,
            )
    log_info(
        "database_initializer_finished",
        "Database initializer finished",
        correlation_id,
        http_status=500 if failed else 200,
        duration_ms=duration_ms,
    )
    reset_correlation_id(token)
    return result
