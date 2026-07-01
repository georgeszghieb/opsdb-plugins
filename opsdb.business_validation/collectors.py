"""OpsDB Business Validation plugin — collectors and connection test.

Executes parameterised SQL checks (row count, null rate, duplicates, freshness)
against PostgreSQL or SQL Server targets using metadata fields from the target.

Requires: psycopg (always available). pymssql for SQL Server targets.
"""

import time
from decimal import Decimal


def test_connection(params: dict) -> dict:
    db_type = str(params.get("db_type", "postgresql")).lower()
    host = str(params.get("host", ""))
    if not host:
        return {"success": False, "message": "Host is required."}
    port = int(params.get("port", 5432 if db_type == "postgresql" else 1433))
    database = str(params.get("database", "")) or None
    user = str(params.get("user", "")) or None
    password = params.get("password") or None

    if db_type == "sqlserver":
        try:
            import pymssql  # type: ignore[import]
        except ImportError:
            return {"success": False, "message": "pymssql not installed. Add pymssql to backend/requirements.txt and rebuild."}
        try:
            t0 = time.perf_counter()
            conn = pymssql.connect(server=host, port=port, database=database, user=user, password=password, login_timeout=5)
            version = None
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT SERVERPROPERTY('ProductVersion')")
                    version = cur.fetchone()[0]
            except Exception:
                pass
            conn.close()
            result = {"success": True, "message": f"Connected to SQL Server {host}:{port}", "latency_ms": int((time.perf_counter() - t0) * 1000)}
            if version:
                result["version"] = str(version)
            return result
        except Exception as exc:
            return {"success": False, "message": str(exc)}

    # Default: PostgreSQL
    try:
        import psycopg
    except ImportError:
        return {"success": False, "message": "psycopg not installed in backend container."}
    try:
        t0 = time.perf_counter()
        conn = psycopg.connect(host=host, port=port, dbname=database, user=user, password=password, connect_timeout=5)
        version = None
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT current_setting('server_version_num')")
                num = int(cur.fetchone()[0])
                version = f"{num // 10000}.{num % 100}"
        except Exception:
            pass
        conn.close()
        result = {"success": True, "message": f"Connected to PostgreSQL {host}:{port}", "latency_ms": int((time.perf_counter() - t0) * 1000)}
        if version:
            result["version"] = version
        return result
    except Exception as exc:
        return {"success": False, "message": str(exc)}


def run_collector(collector_key: str, params: dict, query_params: dict) -> dict:
    query_template = query_params.get("query_template")
    if not query_template:
        raise ValueError(f"No query_template configured for collector '{collector_key}'.")

    metadata_params = query_params.get("metadata_params", [])
    missing = [p for p in metadata_params if not params.get(p)]
    if missing:
        raise ValueError(
            f"Collector '{collector_key}' requires target metadata fields {missing} to be set."
        )

    try:
        query = query_template.format(**{p: params[p] for p in metadata_params})
    except KeyError as exc:
        raise ValueError(f"Query template substitution failed: {exc}")

    db_type = str(params.get("db_type", "postgresql")).lower()
    if db_type == "sqlserver":
        return _run_sqlserver(params, query)
    return _run_postgresql(params, query)


def _run_postgresql(params: dict, query: str) -> dict:
    import psycopg

    host = str(params.get("host", ""))
    port = int(params.get("port", 5432))
    database = str(params.get("database", "")) or None
    user = str(params.get("user", "")) or None
    password = params.get("password") or None

    with psycopg.connect(host=host, port=port, dbname=database, user=user, password=password, connect_timeout=5) as conn:
        conn.autocommit = True
        with conn.cursor() as cursor:
            cursor.execute(query)
            row = cursor.fetchone()
            if row is None:
                return {}
            columns = [col.name for col in cursor.description]
            return _row_to_floats(columns, row)


def _run_sqlserver(params: dict, query: str) -> dict:
    import pymssql  # type: ignore[import]

    host = str(params.get("host", ""))
    port = int(params.get("port", 1433))
    database = str(params.get("database", "")) or None
    user = str(params.get("user", "")) or None
    password = params.get("password") or None

    conn = pymssql.connect(server=host, port=port, database=database, user=user, password=password, login_timeout=5)
    try:
        cursor = conn.cursor(as_dict=True)
        cursor.execute(query)
        row_raw = cursor.fetchone() or {}
        return _row_to_floats(list(row_raw.keys()), list(row_raw.values()))
    finally:
        conn.close()


def _row_to_floats(columns, values) -> dict:
    result = {}
    for col, val in zip(columns, values):
        if val is None:
            continue
        if isinstance(val, Decimal):
            val = float(val)
        try:
            result[col] = float(val)
        except (TypeError, ValueError):
            pass
    return result
