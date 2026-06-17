"""OpsDB Docker Host plugin — collectors and connection test.

Requires: docker SDK — add to backend/requirements.txt to enable.
"""

import time


def test_connection(params: dict) -> dict:
    try:
        import docker  # type: ignore[import]
    except ImportError:
        return {"success": False, "message": "Docker SDK not installed. Add 'docker' to backend/requirements.txt and rebuild."}

    docker_host = params.get("docker_host") or None
    try:
        t0 = time.perf_counter()
        client = docker.DockerClient(base_url=docker_host or "unix:///var/run/docker.sock")
        client.ping()
        client.close()
        return {"success": True, "message": "Docker daemon reachable.", "latency_ms": int((time.perf_counter() - t0) * 1000)}
    except Exception as exc:
        return {"success": False, "message": str(exc)}


def run_collector(collector_key: str, params: dict, query_params: dict) -> dict:
    try:
        import docker  # type: ignore[import]
    except ImportError:
        raise RuntimeError(
            "Docker collector requires the 'docker' SDK. Add docker to backend/requirements.txt and rebuild."
        )

    docker_host = params.get("docker_host") or None
    endpoint = query_params.get("endpoint", "")
    mapping = query_params.get("result_mapping", [])
    units = query_params.get("units", {})

    client = docker.DockerClient(base_url=docker_host or "unix:///var/run/docker.sock")
    try:
        if "/containers/json" in endpoint:
            items = client.containers.list(all=True)
            items_data = [{"Status": c.status, "RestartCount": c.attrs.get("RestartCount", 0)} for c in items]
        elif "/images/json" in endpoint:
            items = client.images.list()
            items_data = [{"Size": i.attrs.get("Size", 0)} for i in items]
        else:
            raise ValueError(f"Unsupported Docker API endpoint: '{endpoint}'")
    finally:
        client.close()

    result = {}
    for rule in mapping:
        metric_name = rule.get("metric_name", "")
        aggregate = rule.get("aggregate", "count")
        field = rule.get("field")
        flt = rule.get("filter", {})

        subset = items_data
        for key, val in flt.items():
            subset = [item for item in subset if item.get(key) == val]

        if aggregate == "count":
            result[metric_name] = float(len(subset))
        elif aggregate == "sum" and field:
            result[metric_name] = float(sum(item.get(field, 0) or 0 for item in subset))

    return result
