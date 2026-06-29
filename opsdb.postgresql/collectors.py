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


_PSS_DEPENDENT_KEYS = {"pg17_query_temp_spills", "pg17_top_queries_pg_stat_statements", "pg17_slow_queries_by_mean"}

# Fallback queries embedded here so collectors work even if config_json is
# stale (plugin registered before these entries were added to the manifest).
_PSS_FALLBACK_QUERIES: dict[str, str] = {
    "pg17_query_temp_spills": (
        "SELECT count(*) FILTER (WHERE temp_blks_read + temp_blks_written > 0) AS queries_with_temp_spill,"
        " COALESCE(max((temp_blks_read + temp_blks_written) * 8.0 / 1024), 0) AS max_temp_spill_mb"
        " FROM pg_stat_statements"
    ),
    "pg17_top_queries_pg_stat_statements": (
        "SELECT substring(query, 1, 500) AS query, calls::int AS calls,"
        " round(mean_exec_time::numeric, 2) AS query_mean_exec_ms,"
        " round(total_exec_time::numeric, 2) AS total_exec_time_ms,"
        " rows::int AS total_rows"
        " FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 25"
    ),
    "pg17_slow_queries_by_mean": (
        "SELECT substring(query, 1, 500) AS query, calls::int AS calls,"
        " round(mean_exec_time::numeric, 2) AS mean_exec_time_ms,"
        " round(stddev_exec_time::numeric, 2) AS stddev_exec_time_ms,"
        " round(total_exec_time::numeric, 2) AS total_exec_time_ms,"
        " rows::int AS total_rows,"
        " round(shared_blks_read::numeric / NULLIF(shared_blks_hit + shared_blks_read, 0) * 100, 1) AS cache_miss_pct"
        " FROM pg_stat_statements WHERE calls > 3 ORDER BY mean_exec_time DESC LIMIT 25"
    ),
    "pg17_log_entries": (
        "WITH latest_log AS ("
        " SELECT current_setting('log_directory') || '/' || name AS log_file_path,"
        "        name AS log_file, modification AS last_modified, size::bigint AS size_bytes"
        " FROM pg_ls_logdir()"
        " WHERE name ~ '\\.(log|csv|json)$'"
        " ORDER BY modification DESC LIMIT 1"
        ")"
        " SELECT log_file, log_file_path, last_modified,"
        "        pg_size_pretty(size_bytes) AS file_size,"
        "        pg_read_file(log_file_path, GREATEST(0, size_bytes - 32768), LEAST(size_bytes, 32768)) AS recent_lines"
        " FROM latest_log WHERE size_bytes > 0"
    ),
}


def run_collector(collector_key: str, params: dict, query_params: dict) -> dict:
    try:
        import psycopg
    except ImportError:
        raise RuntimeError("psycopg not installed in backend container.")

    env_name = params.get("connection_env")
    if env_name:
        dsn = os.getenv(str(env_name))
        if dsn:
            with psycopg.connect(dsn, connect_timeout=5) as conn:
                return _dispatch(conn, collector_key, query_params)

    host = str(params.get("host", ""))
    if not host:
        raise ValueError("No host configured for PostgreSQL target.")

    port = int(params.get("port", 5432))
    database = params.get("database") or None
    user = params.get("user") or None
    password = params.get("password") or None

    with psycopg.connect(host=host, port=port, dbname=database, user=user, password=password, connect_timeout=5) as conn:
        return _dispatch(conn, collector_key, query_params)


_LOG_READER_KEYS = {"pg17_log_entries"}


def _dispatch(conn, collector_key: str, query_params: dict) -> dict:
    if collector_key in _LOG_READER_KEYS:
        return _read_log_entries(conn, query_params)

    # Use config_json query first; fall back to embedded query for collectors
    # where config_json may be stale (old plugin install).
    query = query_params.get("query") or _PSS_FALLBACK_QUERIES.get(collector_key)
    if not query:
        raise ValueError(f"No query configured for collector '{collector_key}'.")

    if collector_key in _PSS_DEPENDENT_KEYS:
        return _execute_query_pss(conn, query)

    return _execute_query(conn, query)


def _execute_query_pss(conn, query: str) -> dict:
    """Run a query that depends on pg_stat_statements. Returns a friendly empty result
    instead of failing if the extension is not installed."""
    conn.autocommit = True
    with conn.cursor() as cursor:
        try:
            cursor.execute("SELECT 1 FROM pg_catalog.pg_extension WHERE extname = 'pg_stat_statements'")
            if not cursor.fetchone():
                return {
                    "_rows": [{"status": "pg_stat_statements extension is not installed",
                               "fix": "Run: CREATE EXTENSION pg_stat_statements; and add it to shared_preload_libraries"}],
                    "_columns": ["status", "fix"],
                }
        except Exception:
            pass  # pg_catalog is always available; ignore any unexpected error
    return _execute_query(conn, query)


# ---------------------------------------------------------------------------
# Stateful log reader — PostgreSQL transport
# ---------------------------------------------------------------------------
# The engine (chunking, rotation detection, state tracking, zero-loss
# guarantees) lives in app.utils.log_reader — shared across all plugins.
# This section provides only the two PostgreSQL-specific transport functions:
#   list_files  — discovers log files via pg_ls_logdir()
#   read_bytes  — reads file content via pg_read_file()
# ---------------------------------------------------------------------------

def _read_log_entries(conn, query_params: dict) -> dict:
    """PostgreSQL log collector — thin transport wrapper over the shared engine."""
    from opsdb_sdk.log_reader import run_log_reader

    conn.autocommit = True

    def list_files() -> list[dict]:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT current_setting('log_directory') || '/' || name AS path,
                       name,
                       size::bigint AS size
                FROM pg_ls_logdir()
                WHERE name ~ '\\.(log|csv|json)$'
                ORDER BY modification DESC
                LIMIT 5
            """)
            cols = [c.name for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def read_bytes(path: str, offset: int, size: int) -> str:
        if size <= 0:
            return ""
        with conn.cursor() as cur:
            cur.execute("SELECT pg_read_file(%s, %s, %s)", [path, offset, size])
            return cur.fetchone()[0] or ""

    return run_log_reader(
        prev_state=query_params.get("_prev_state") or {},
        list_files=list_files,
        read_bytes=read_bytes,
    )


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
