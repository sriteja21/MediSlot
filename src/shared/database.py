"""
MediSlot shared database utilities.

Provides secure RDS MySQL connection management and transaction handling.

Environment variables:
    DB_HOST
    DB_PORT
    DB_NAME
    DB_PASSWORD_PARAMETER_NAME          (or DATABASE_CREDENTIALS_PARAMETER_NAME)
    DB_USERNAME                         (only needed when the SSM parameter
                                         holds a plain password)

The SSM parameter may contain either:
    - a JSON object: {"username": "...", "password": "..."}
    - a plain password string (DB_USERNAME must then be set)
"""

import json
import os
from contextlib import contextmanager
from typing import Any, Iterator

import pymysql
from pymysql.connections import Connection

from shared.config import get_required_env, get_ssm_parameter


def _resolve_credentials() -> tuple[str, str]:
    """
    Resolve the database username and password.

    Never logs or returns secrets anywhere except to the caller.
    """
    parameter_name = (
        os.environ.get("DB_PASSWORD_PARAMETER_NAME")
        or os.environ.get("DATABASE_CREDENTIALS_PARAMETER_NAME")
    )

    if not parameter_name:
        raise RuntimeError(
            "Neither DB_PASSWORD_PARAMETER_NAME nor "
            "DATABASE_CREDENTIALS_PARAMETER_NAME is configured."
        )

    raw_value = get_ssm_parameter(
        parameter_name=parameter_name,
        decrypt=True,
    )

    env_username = os.environ.get("DB_USERNAME", "").strip()

    try:
        parsed = json.loads(raw_value)
    except (json.JSONDecodeError, TypeError):
        parsed = None

    if isinstance(parsed, dict):
        username = (
            parsed.get("username")
            or parsed.get("user")
            or env_username
        )
        password = parsed.get("password")
    else:
        username = env_username
        password = raw_value

    if not username:
        raise RuntimeError(
            "Database username is not configured. Set DB_USERNAME or "
            "store JSON with a 'username' key in the SSM parameter."
        )

    if not password:
        raise RuntimeError(
            "Database password is missing from the SSM parameter."
        )

    return str(username), str(password)


def get_database_connection() -> Connection:
    """
    Create a connection to the MediSlot RDS MySQL database.

    The Lambda execution role must be allowed to read the configured
    SSM parameter (and decrypt it with KMS if it uses a CMK).
    """
    db_host = get_required_env("DB_HOST")
    db_port = int(get_required_env("DB_PORT"))
    db_name = get_required_env("DB_NAME")

    db_username, db_password = _resolve_credentials()

    return pymysql.connect(
        host=db_host,
        port=db_port,
        user=db_username,
        password=db_password,
        database=db_name,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
        connect_timeout=5,
        read_timeout=10,
        write_timeout=10,
        ssl={"ssl": True},
    )


@contextmanager
def database_connection() -> Iterator[Connection]:
    """
    Yield a database connection and always close it afterward.

    Use with a `with` statement. The caller is responsible for
    committing or rolling back transactions.
    """
    connection = get_database_connection()

    try:
        yield connection
    finally:
        connection.close()


@contextmanager
def database_transaction() -> Iterator[Connection]:
    """
    Manage a database transaction.

    On success:
        Commit the transaction.

    On failure:
        Roll back the transaction and re-raise the exception.

    Example:
        with database_transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO example (id) VALUES (%s)",
                    (record_id,),
                )
    """
    with database_connection() as connection:
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def execute_query(
    query: str,
    parameters: tuple | dict | None = None,
) -> list[dict[str, Any]]:
    """
    Execute a SELECT query and return its rows.

    Always use parameterized SQL for user-supplied values.
    Do not construct SQL by concatenating user input.
    """
    with database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(query, parameters or ())
            return list(cursor.fetchall())


def execute_write(
    query: str,
    parameters: tuple | dict | None = None,
) -> int:
    """
    Execute a single INSERT, UPDATE, or DELETE statement.

    Returns:
        The number of affected rows.

    The statement is committed only if execution succeeds.
    """
    with database_transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(query, parameters or ())
            return cursor.rowcount