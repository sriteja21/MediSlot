"""
MediSlot shared database utilities.

Provides secure RDS MySQL connection management and transaction handling.

Required environment variables:
    DB_HOST
    DB_PORT
    DB_NAME
    DB_USERNAME
    DB_PASSWORD_PARAMETER_NAME
"""

from contextlib import contextmanager
from typing import Any, Iterator

import pymysql
from pymysql.connections import Connection

from shared.config import get_required_env, get_ssm_parameter


def get_database_connection() -> Connection:
    """
    Create a connection to the MediSlot RDS MySQL database.

    The database password is retrieved from SSM Parameter Store.
    The Lambda execution role must have permission to read the
    configured parameter.
    """
    db_host = get_required_env("DB_HOST")
    db_port = int(get_required_env("DB_PORT"))
    db_name = get_required_env("DB_NAME")
    db_username = get_required_env("DB_USERNAME")
    password_parameter_name = get_required_env(
        "DB_PASSWORD_PARAMETER_NAME"
    )

    db_password = get_ssm_parameter(
        parameter_name=password_parameter_name,
        decrypt=True,
    )

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

    The caller is responsible for committing or rolling back
    transactions when using this context manager.
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
