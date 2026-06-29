"""OpsDB Windows Server plugin — collectors v2.0.0

Requires: pywinrm — add to backend/requirements.txt to enable.

Standard interface consumed by plugin_loader:
    test_connection(params: dict) -> dict
    run_collector(collector_key: str, params: dict, query_params: dict) -> dict
"""

import json
import socket
import time


# ---------------------------------------------------------------------------
# Connection test — TCP reachability on WinRM port
# ---------------------------------------------------------------------------

def test_connection(params: dict) -> dict:
    host = str(params.get("host", ""))
    if not host:
        return {"success": False, "message": "Host is required."}
    port = int(params.get("winrm_port", 5985))
    try:
        t0 = time.perf_counter()
        s = socket.create_connection((host, port), timeout=5)
        s.close()
        latency = int((time.perf_counter() - t0) * 1000)
    except Exception as exc:
        return {"success": False, "message": str(exc)}

    # Attempt a live WinRM auth test if credentials are provided
    password = params.get("password") or ""
    username = str(params.get("username", "Administrator"))
    domain = str(params.get("domain", "")).strip()
    transport = str(params.get("auth_transport", "ntlm")).lower()
    use_ssl = str(params.get("use_ssl", "no")).lower() == "yes"
    auth_user = f"{domain}\\{username}" if domain else username
    protocol = "https" if use_ssl else "http"

    if password:
        try:
            import winrm  # type: ignore[import]
            session = winrm.Session(
                f"{protocol}://{host}:{port}/wsman",
                auth=(auth_user, password),
                transport=transport,
                server_cert_validation="ignore",
                operation_timeout_sec=10,
                read_timeout_sec=12,
            )
            result = session.run_ps("$PSVersionTable.PSVersion.Major")
            if result.status_code == 0:
                ps_ver = result.std_out.decode("utf-8", errors="replace").strip()
                return {
                    "success": True,
                    "message": f"WinRM authenticated ({transport.upper()}) on {host}:{port} — PowerShell {ps_ver}",
                    "latency_ms": latency,
                }
            err = result.std_err.decode("utf-8", errors="replace").strip()
            return {"success": False, "message": f"WinRM auth OK but PS command failed: {err[:200]}", "latency_ms": latency}
        except Exception as exc:
            msg = str(exc)
            if "rejected" in msg.lower() or "401" in msg or "unauthorized" in msg.lower():
                display_user = auth_user
                if transport == "basic":
                    hint = (
                        f" — Basic auth rejected (user: '{display_user}'). "
                        "If Basic=true and AllowUnencrypted=true are confirmed on the target, "
                        "the credentials stored in OpsDB are likely wrong. "
                        "Verify the password in Settings → Credentials, or test locally on the Windows host: "
                        "winrm identify -r:http://localhost:5985 -a:basic "
                        f"-u:{display_user} -p:YourPassword"
                    )
                elif transport == "ntlm":
                    hint = (
                        f" — NTLM auth rejected (user: '{display_user}'). "
                        "Enable NTLM on the target: Set-Item WSMan:\\localhost\\Service\\Auth\\Ntlm -Value $true, "
                        "or verify the credentials are correct."
                    )
                else:
                    hint = f" — {transport} auth rejected (user: '{display_user}')."
                return {"success": False, "message": f"WinRM reachable but auth failed{hint}", "latency_ms": latency}
            return {"success": False, "message": f"WinRM error: {msg[:300]}", "latency_ms": latency}

    return {
        "success": True,
        "message": f"WinRM port {port} reachable on {host} (no credential provided to test auth)",
        "latency_ms": latency,
    }


# ---------------------------------------------------------------------------
# Collector runner — WinRM PowerShell execution
# ---------------------------------------------------------------------------

def run_collector(collector_key: str, params: dict, query_params: dict) -> dict:
    try:
        import winrm  # type: ignore[import]
    except ImportError:
        raise RuntimeError(
            "Windows collector requires 'pywinrm'. "
            "Add 'pywinrm' to backend/requirements.txt and rebuild the backend container."
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
    domain = str(params.get("domain", "")).strip()
    transport = str(params.get("auth_transport", "ntlm")).lower()

    # Domain-prefix the username when a domain is specified
    auth_user = f"{domain}\\{username}" if domain else username
    protocol = "https" if use_ssl else "http"

    session = winrm.Session(
        f"{protocol}://{host}:{port}/wsman",
        auth=(auth_user, password),
        transport=transport,
        server_cert_validation="ignore",
        operation_timeout_sec=30,
        read_timeout_sec=35,
    )

    try:
        result = session.run_ps(command)
    except Exception as exc:
        msg = str(exc)
        if "rejected" in msg.lower() or "401" in msg or "unauthorized" in msg.lower():
            if transport == "basic":
                fix = (
                    f"Basic auth rejected (user: '{auth_user}'). "
                    "Confirm Basic=true and AllowUnencrypted=true on the target, then verify "
                    "the credentials stored in OpsDB match the Windows account password exactly."
                )
            elif transport == "ntlm":
                fix = (
                    f"NTLM auth rejected (user: '{auth_user}'). "
                    "Enable NTLM on the target: Set-Item WSMan:\\localhost\\Service\\Auth\\Ntlm -Value $true, "
                    "or verify the credentials are correct."
                )
            else:
                fix = f"Auth transport '{transport}' rejected (user: '{auth_user}')."
            raise ValueError(f"WinRM auth failed on {host}: {fix}") from exc
        raise ValueError(f"WinRM connection error: {msg[:300]}") from exc

    if result.status_code != 0:
        err = result.std_err.decode("utf-8", errors="replace").strip()
        raise ValueError(
            f"WinRM PowerShell command failed (exit {result.status_code}): {err[:400]}"
        )

    output = result.std_out.decode("utf-8", errors="replace").strip()
    if not output:
        raise ValueError("WinRM command produced no output.")

    try:
        data = json.loads(output)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"WinRM output is not valid JSON: {exc}. "
            f"Output snippet: {output[:200]}"
        )

    return _parse_result(data)


# ---------------------------------------------------------------------------
# Result normalisation
# ---------------------------------------------------------------------------

def _parse_result(data) -> dict:
    """Parse PowerShell JSON output into the standard OpsDB collector result format.

    Handles:
      - dict  → flat key/value pairs (e.g. {cpu_usage_percent: 45.2})
      - list  → tabular rows (e.g. [{drive: 'C', used_pct: 70}, ...])

    Returns:
      {metric_name: float}  — numeric values from the first row/object → MetricSamples
      "_rows": [...]        — all rows as string-valued dicts for UI display
      "_columns": [...]     — ordered column names
    """
    if isinstance(data, list):
        if not data:
            return {"_rows": [], "_columns": []}
        rows = data
        columns = list(rows[0].keys()) if rows else []
        result: dict = {}
        # Extract numeric metrics from the first row only
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
        # Expose as a single display row so collectors show up in the results grid
        result["_rows"] = [_stringify_row(data)]
        result["_columns"] = list(data.keys())
        return result

    raise ValueError(
        f"Unexpected JSON root type from PowerShell: {type(data).__name__}. "
        "Commands must return a JSON object or JSON array."
    )


def _stringify_row(row: dict) -> dict:
    """Convert all values in a row dict to display strings."""
    return {k: ("" if v is None else str(v)) for k, v in row.items()}


def _coerce_numeric(value) -> "float | None":
    """Convert a JSON-decoded value to float for MetricSample storage.

    Handles the types PowerShell commonly returns via ConvertTo-Json:
      bool   → 0.0 / 1.0
      int    → float
      float  → float
      str    → float if parseable, otherwise None (display-only)
      None   → None (skipped)
    """
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
