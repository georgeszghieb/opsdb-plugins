"""OpsDB Windows Server plugin — collectors and connection test.

Requires: pywinrm — add to backend/requirements.txt to enable.
"""

import json
import socket
import time


def test_connection(params: dict) -> dict:
    host = str(params.get("host", ""))
    if not host:
        return {"success": False, "message": "Host is required."}
    port = int(params.get("winrm_port", 5985))
    try:
        t0 = time.perf_counter()
        s = socket.create_connection((host, port), timeout=5)
        s.close()
        return {"success": True, "message": f"WinRM port {port} reachable on {host}", "latency_ms": int((time.perf_counter() - t0) * 1000)}
    except Exception as exc:
        return {"success": False, "message": str(exc)}


def run_collector(collector_key: str, params: dict, query_params: dict) -> dict:
    try:
        import winrm  # type: ignore[import]
    except ImportError:
        raise RuntimeError(
            "Windows collector requires 'pywinrm'. Add pywinrm to backend/requirements.txt and rebuild."
        )

    command = query_params.get("command")
    if not command:
        raise ValueError(f"No command configured for collector '{collector_key}'.")

    host = str(params.get("host", ""))
    if not host:
        raise ValueError("No host configured for Windows target.")

    port = int(params.get("winrm_port", 5985))
    username = str(params.get("username", "Administrator"))
    password = params.get("password") or ""
    use_ssl = str(params.get("use_ssl", "no")).lower() == "yes"

    protocol = "https" if use_ssl else "http"
    session = winrm.Session(
        f"{protocol}://{host}:{port}/wsman",
        auth=(username, password),
        transport="ntlm",
        server_cert_validation="ignore",
    )
    result = session.run_ps(command)
    if result.status_code != 0:
        err = result.std_err.decode("utf-8", errors="replace").strip()
        raise ValueError(f"WinRM command failed (exit {result.status_code}): {err[:300]}")

    output = result.std_out.decode("utf-8", errors="replace").strip()
    if not output:
        raise ValueError("WinRM command produced no output.")

    result_format = query_params.get("result_format", "json")
    if result_format != "json":
        raise ValueError(f"Unsupported result_format: '{result_format}'")

    try:
        data = json.loads(output)
    except json.JSONDecodeError as exc:
        raise ValueError(f"WinRM output is not valid JSON: {exc}. Output: {output[:200]}")

    return {k: float(v) for k, v in data.items() if v is not None}
