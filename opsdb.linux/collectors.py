"""OpsDB Linux Server plugin — collectors and connection test.

Requires: paramiko — available in the OpsDB backend container.
"""

import json
import socket
import time


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
            return {"success": True, "message": f"Port {port} reachable on {host} (paramiko not installed — full auth test skipped)", "latency_ms": int((time.perf_counter() - t0) * 1000)}
        except Exception as exc:
            return {"success": False, "message": str(exc)}

    username = str(params.get("username", "root"))
    password = params.get("password")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        t0 = time.perf_counter()
        client.connect(hostname=host, port=port, username=username, password=password, timeout=10)
        latency_ms = int((time.perf_counter() - t0) * 1000)
        return {"success": True, "message": f"SSH connected to {host}:{port}", "latency_ms": latency_ms}
    except Exception as exc:
        return {"success": False, "message": str(exc)}
    finally:
        client.close()


def run_collector(collector_key: str, params: dict, query_params: dict) -> dict:
    try:
        import paramiko  # type: ignore[import]
    except ImportError:
        raise RuntimeError(
            "Linux collector requires 'paramiko'. Add paramiko to backend/requirements.txt and rebuild."
        )

    command = query_params.get("command")
    if not command:
        raise ValueError(f"No command configured for collector '{collector_key}'.")

    host = str(params.get("host", ""))
    if not host:
        raise ValueError("No host configured for Linux target.")

    port = int(params.get("port", 22))
    username = str(params.get("username", "root"))
    password = params.get("password")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(hostname=host, port=port, username=username, password=password, timeout=10)
        _stdin, stdout, stderr = client.exec_command(command, timeout=30)
        output = stdout.read().decode("utf-8", errors="replace").strip()
        err = stderr.read().decode("utf-8", errors="replace").strip()
    finally:
        client.close()

    if not output:
        raise ValueError(f"SSH command produced no output. Stderr: {err[:200]}")

    result_format = query_params.get("result_format", "json")
    if result_format != "json":
        raise ValueError(f"Unsupported result_format: '{result_format}'")

    try:
        data = json.loads(output)
    except json.JSONDecodeError as exc:
        raise ValueError(f"SSH output is not valid JSON: {exc}. Output: {output[:200]}")

    return {k: float(v) for k, v in data.items() if v is not None}
