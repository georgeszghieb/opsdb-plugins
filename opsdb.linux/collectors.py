"""OpsDB Linux Server plugin — collectors v2.0.0

Requires: paramiko — available in the OpsDB backend container.

Standard interface consumed by plugin_loader:
    test_connection(params: dict) -> dict
    run_collector(collector_key: str, params: dict, query_params: dict) -> dict
"""

import json
import socket
import time


# ---------------------------------------------------------------------------
# Connection test — SSH reachability and optional auth
# ---------------------------------------------------------------------------

def test_connection(params: dict) -> dict:
    host = str(params.get("host", ""))
    if not host:
        return {"success": False, "message": "Host is required."}
    port = int(params.get("port", 22))
    try:
        import paramiko  # type: ignore[import]
    except ImportError:
        try:
            t0 = time.perf_counter()
            s = socket.create_connection((host, port), timeout=5)
            s.close()
            latency = int((time.perf_counter() - t0) * 1000)
            return {
                "success": True,
                "message": f"Port {port} reachable on {host} (paramiko not installed — full auth test skipped)",
                "latency_ms": latency,
            }
        except Exception as exc:
            return {"success": False, "message": str(exc)}

    username = str(params.get("username", "ubuntu"))
    password = params.get("password")
    auth_type = str(params.get("auth_type", "password")).lower()

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        t0 = time.perf_counter()
        if auth_type == "key" and password:
            import io
            pkey = _load_private_key(password)
            client.connect(
                hostname=host, port=port, username=username,
                pkey=pkey, look_for_keys=False, allow_agent=False, timeout=10,
            )
        else:
            client.connect(
                hostname=host, port=port, username=username,
                password=password, look_for_keys=False, allow_agent=False, timeout=10,
            )
        latency_ms = int((time.perf_counter() - t0) * 1000)
        # Quick sanity check — confirm Python 3 is available
        _stdin, stdout, _stderr = client.exec_command("python3 --version", timeout=5)
        py_ver = stdout.read().decode("utf-8", errors="replace").strip()
        # Distro version (e.g. "22.04" for Ubuntu) — same source as linux.system_info's os_version
        _stdin, ver_stdout, _stderr = client.exec_command(
            "grep '^VERSION_ID=' /etc/os-release 2>/dev/null | cut -d= -f2 | tr -d '\"'", timeout=5,
        )
        os_version = ver_stdout.read().decode("utf-8", errors="replace").strip()
        response = {
            "success": True,
            "message": f"SSH connected to {host}:{port} — {py_ver or 'Python 3 present'}",
            "latency_ms": latency_ms,
        }
        if os_version:
            response["version"] = os_version
        return response
    except Exception as exc:
        msg = str(exc)
        if "authentication" in msg.lower() or "auth" in msg.lower() or "no acceptable" in msg.lower():
            if auth_type == "key":
                hint = (
                    f" — Key authentication rejected for user '{username}'. "
                    "Ensure the public key is in ~/.ssh/authorized_keys on the target, "
                    "and that the private key stored in OpsDB is correct."
                )
            else:
                hint = (
                    f" — Password authentication rejected for user '{username}'. "
                    "Verify the credentials stored in OpsDB Settings → Credentials match "
                    "the account password on the target server."
                )
            return {"success": False, "message": f"SSH auth failed on {host}:{port}{hint}"}
        return {"success": False, "message": f"SSH connection error: {msg[:300]}"}
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Collector runner — SSH command execution
# ---------------------------------------------------------------------------

def run_collector(collector_key: str, params: dict, query_params: dict) -> dict:
    try:
        import paramiko  # type: ignore[import]
    except ImportError:
        raise RuntimeError(
            "Linux collector requires 'paramiko'. "
            "Add paramiko to backend/requirements.txt and rebuild the backend container."
        )

    command = query_params.get("command")
    if not command:
        raise ValueError(f"No command configured for collector '{collector_key}'.")

    host = str(params.get("host", ""))
    if not host:
        raise ValueError("No host configured for Linux target.")

    port = int(params.get("port", 22))
    username = str(params.get("username", "ubuntu"))
    password = params.get("password")
    auth_type = str(params.get("auth_type", "password")).lower()

    client = _connect(host, port, username, password, auth_type)
    try:
        _stdin, stdout, stderr = client.exec_command(command, timeout=35)
        output = stdout.read().decode("utf-8", errors="replace").strip()
        err = stderr.read().decode("utf-8", errors="replace").strip()
    finally:
        client.close()

    if not output:
        raise ValueError(
            f"SSH command for '{collector_key}' produced no output. "
            f"Stderr: {err[:300]}"
        )

    result_format = query_params.get("result_format", "json")
    if result_format != "json":
        raise ValueError(f"Unsupported result_format: '{result_format}'")

    try:
        data = json.loads(output)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"SSH output for '{collector_key}' is not valid JSON: {exc}. "
            f"Output snippet: {output[:300]}"
        )

    return _parse_result(data)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _connect(host: str, port: int, username: str, password, auth_type: str):
    import paramiko  # type: ignore[import]
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    if auth_type == "key" and password:
        pkey = _load_private_key(password)
        client.connect(
            hostname=host, port=port, username=username,
            pkey=pkey, look_for_keys=False, allow_agent=False, timeout=10,
        )
    else:
        client.connect(
            hostname=host, port=port, username=username,
            password=password, look_for_keys=False, allow_agent=False, timeout=10,
        )
    return client


def _load_private_key(key_text: str):
    """Try to load a PEM private key from the credential value (RSA, Ed25519, ECDSA)."""
    import io
    import paramiko  # type: ignore[import]
    key_io = io.StringIO(key_text.strip())
    for cls in (paramiko.RSAKey, paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.DSSKey):
        try:
            key_io.seek(0)
            return cls.from_private_key(key_io)
        except Exception:
            continue
    raise ValueError(
        "Could not load the private key stored in the credential. "
        "Ensure it is a valid unencrypted PEM private key (RSA, Ed25519, or ECDSA)."
    )


def _parse_result(data) -> dict:
    """Parse SSH JSON output into the standard OpsDB collector result format.

    Handles:
      - dict  → flat key/value pairs  → MetricSamples
      - list  → tabular rows          → _rows / _columns for UI display

    Returns:
      {metric_name: float}  — numeric values from the first row/object
      "_rows": [...]        — all rows as string-valued dicts for display
      "_columns": [...]     — ordered column names
    """
    if isinstance(data, list):
        if not data:
            return {"_rows": [], "_columns": []}
        rows = data
        columns = list(rows[0].keys()) if rows else []
        result: dict = {}
        for k, v in rows[0].items():
            coerced = _coerce_numeric(v)
            if coerced is not None:
                result[k] = coerced
        result["_rows"] = [_stringify_row(r) for r in rows]
        result["_columns"] = columns
        return result

    if isinstance(data, dict):
        result = {}
        for k, v in data.items():
            coerced = _coerce_numeric(v)
            if coerced is not None:
                result[k] = coerced
        result["_rows"] = [_stringify_row(data)]
        result["_columns"] = list(data.keys())
        return result

    raise ValueError(
        f"Unexpected JSON root type from SSH command: {type(data).__name__}. "
        "Commands must return a JSON object or JSON array."
    )


def _stringify_row(row: dict) -> dict:
    return {k: ("" if v is None else str(v)) for k, v in row.items()}


def _coerce_numeric(value) -> "float | None":
    """Convert a JSON value to float for MetricSample storage."""
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return None
