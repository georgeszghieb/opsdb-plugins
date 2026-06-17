"""OpsDB SQL Server plugin — collectors and connection test.

Requires: pymssql — add to backend/requirements.txt to enable.
"""

import socket
import time
from decimal import Decimal


def test_connection(params: dict) -> dict:
    host = str(params.get("host", ""))
    if not host:
        return {"success": False, "message": "Host is required."}
    port = int(params.get("port", 1433))
    try:
        import pymssql  # type: ignore[import]
    except ImportError:
        # Fall back to TCP port probe when pymssql is not installed
        try:
            t0 = time.perf_counter()
            s = socket.create_connection((host, port), timeout=5)
            s.close()
            return {"success": True, "message": f"Port {port} reachable on {host} (pymssql not installed — full auth test skipped)", "latency_ms": int((time.perf_counter() - t0) * 1000)}
        except Exception as exc:
            return {"success": False, "message": str(exc)}

    database = str(params.get("database", "")) or None
    user = str(params.get("user", "")) or None
    password = params.get("password") or None
    try:
        t0 = time.perf_counter()
        conn = pymssql.connect(server=host, port=port, database=database, user=user, password=password, login_timeout=5)
        conn.close()
        return {"success": True, "message": f"Connected to {host}:{port}", "latency_ms": int((time.perf_counter() - t0) * 1000)}
    except Exception as exc:
        return {"success": False, "message": str(exc)}


def run_collector(collector_key: str, params: dict, query_params: dict) -> dict:
    try:
        import pymssql  # type: ignore[import]
    except ImportError:
        raise RuntimeError(
            "SQL Server collector requires 'pymssql'. Add pymssql to backend/requirements.txt and rebuild."
        )

    query = query_params.get("query")
    if not query:
        raise ValueError(f"No query configured for collector '{collector_key}'.")

    host = str(params.get("host", ""))
    if not host:
        raise ValueError("No host configured for SQL Server target.")

    port = int(params.get("port", 1433))
    database = str(params.get("database", "")) or None
    user = str(params.get("user", "")) or None
    password = params.get("password") or None

    conn = pymssql.connect(server=host, port=port, database=database, user=user, password=password, login_timeout=5)
    try:
        cursor = conn.cursor(as_dict=True)
        cursor.execute(query)
        row_raw = cursor.fetchone() or {}
        result = {}
        for k, v in row_raw.items():
            if v is None:
                continue
            if isinstance(v, Decimal):
                v = float(v)
            try:
                result[k] = float(v)
            except (TypeError, ValueError):
                pass
        return result
    finally:
        conn.close()
