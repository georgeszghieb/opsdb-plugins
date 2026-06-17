"""OpsDB PostgreSQL plugin — collectors.py v1.1.0

Requires: psycopg (psycopg3) — available in the OpsDB backend container.

Standard interface consumed by plugin_loader:
    test_connection(params: dict) -> dict
    run_collector(collector_key: str, params: dict, query_params: dict) -> dict
"""

import datetime
import os
import time
from decimal import Decimal


def test_connection(params: dict) -> dict:
    try:
        import psycopg
    except ImportError:
        return {"success": False, "message": "psycopg not installed in backend container."}

    env_name = params.get("connection_env")
    if env_name:
        dsn = os.getenv(str(env_name))
        if dsn:
            try:
                t0 = time.perf_counter()
                conn = psycopg.connect(dsn, connect_timeout=5)
                conn.close()
                return {"success": True, "message": f"Connected via env var {env_name}", "latency_ms": int((time.perf_counter() - t0) * 1000)}
            except Exception as exc:
                return {"success": False, "message": str(exc)}

    host = str(params.get("host", ""))
    if not host:
        return {"success": False, "message": "Host is required."}

    port = int(params.get("port", 5432))
    database = params.get("database") or None
    user = params.get("user") or None
    password = params.get("password") or None

    note = None
    if params.get("_host_translated"):
        note = (
            "Host was translated to 'host.docker.internal' because the backend runs inside Docker. "
            "Save the target with host 'host.docker.internal' so collectors also work correctly."
        )

    try:
        t0 = time.perf_counter()
        conn = psycopg.connect(host=host, port=port, dbname=database, user=user, password=password, connect_timeout=5)
        conn.close()
        result = {"success": True, "message": f"Connected to {host}:{port}", "latency_ms": int((time.perf_counter() - t0) * 1000)}
        if note:
            result["note"] = note
        return result
    except Exception as exc:
        result = {"success": False, "message": str(exc)}
        if note:
            result["note"] = note
        return result


def run_collector(collector_key: str, params: dict, query_params: dict) -> dict:
    try:
        import psycopg
    except ImportError:
        raise RuntimeError("psycopg not installed in backend container.")

    query = query_params.get("query")
    if not query:
        raise ValueError(f"No query configured for collector '{collector_key}'.")

    env_name = params.get("connection_env")
    if env_name:
        dsn = os.getenv(str(env_name))
        if dsn:
            with psycopg.connect(dsn, connect_timeout=5) as conn:
                return _execute_query(conn, query)

    host = str(params.get("host", ""))
    if not host:
        raise ValueError("No host configured for PostgreSQL target.")

    port = int(params.get("port", 5432))
    database = params.get("database") or None
    user = params.get("user") or None
    password = params.get("password") or None

    with psycopg.connect(host=host, port=port, dbname=database, user=user, password=password, connect_timeout=5) as conn:
        return _execute_query(conn, query)


def _execute_query(conn, query: str) -> dict:
    """Execute a query and return metric values plus raw rows for display.

    Returns a dict with:
      - {column_name: float} for all numerically coercible values in the first row
        (these become MetricSample records in the platform)
      - "_rows": list of {column: string_value} for all rows (for UI display)
      - "_columns": ordered list of column names
    """
    conn.autocommit = True
    with conn.cursor() as cursor:
        cursor.execute(query)
        columns = [col.name for col in cursor.description]
        rows = cursor.fetchall()
        if not rows:
            return {"_rows": [], "_columns": columns}

        # Numeric aggregates from first row — stored as MetricSamples
        result: dict = {}
        for i, value in enumerate(rows[0]):
            coerced = _coerce_numeric(value)
            if coerced is not None:
                result[columns[i]] = coerced

        # All rows as string dicts — stored in result_json for UI display
        result["_rows"] = [
            {columns[i]: _format_display_value(v) for i, v in enumerate(row)}
            for row in rows
        ]
        result["_columns"] = columns
        return result


def _format_display_value(value) -> str:
    """Convert any PostgreSQL value to a display string for the results grid."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        f = float(value)
        return f"{f:.4f}".rstrip("0").rstrip(".")
    if isinstance(value, datetime.timedelta):
        return f"{value.total_seconds():.3f}s"
    if isinstance(value, datetime.datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, (datetime.date, datetime.time)):
        return str(value)
    return str(value)


def _coerce_numeric(value) -> float | None:
    """Convert a psycopg value to float for metric storage.

    Handles the types that PostgreSQL collectors commonly return:
    - bool (pg_is_in_recovery, rolsuper, etc.)  → 0.0 / 1.0
    - Decimal (round(), numeric casts)           → float
    - timedelta (replay_lag, query_age, uptime)  → total_seconds()
    - int / float                                → float
    - str, datetime, inet, None                  → None (skipped)
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime.timedelta):
        return value.total_seconds()
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return None
    if isinstance(value, str):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
