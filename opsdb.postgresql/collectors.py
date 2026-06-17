"""OpsDB PostgreSQL plugin — collectors and connection test.

Requires: psycopg (psycopg3) — available in the OpsDB backend container.

Standard interface consumed by plugin_loader:
    test_connection(params: dict) -> dict
    run_collector(collector_key: str, params: dict, query_params: dict) -> dict
"""

import os
import time
from decimal import Decimal


def test_connection(params: dict) -> dict:
    try:
        import psycopg
    except ImportError:
        return {"success": False, "message": "psycopg not installed in backend container."}

    # DSN env var override takes priority
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
            f"Host was translated to 'host.docker.internal' because the backend runs inside Docker. "
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

    # DSN env var override
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
    conn.autocommit = True
    with conn.cursor() as cursor:
        cursor.execute(query)
        row = cursor.fetchone()
        if row is None:
            return {}
        columns = [col.name for col in cursor.description]
        result = {}
        for i, value in enumerate(row):
            if value is None:
                continue
            if isinstance(value, Decimal):
                value = float(value)
            try:
                result[columns[i]] = float(value)
            except (TypeError, ValueError):
                pass
        return result
