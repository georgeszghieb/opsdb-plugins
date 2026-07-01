"""OpsDB Kubernetes Cluster plugin — collectors and connection test.

Requires: kubernetes client — add to backend/requirements.txt to enable.
"""

import os
import time


def test_connection(params: dict) -> dict:
    try:
        from kubernetes import client as k8s_client, config as k8s_config  # type: ignore[import]
    except ImportError:
        return {"success": False, "message": "Kubernetes client not installed. Add 'kubernetes' to backend/requirements.txt and rebuild."}

    try:
        _load_k8s_config(params)
        t0 = time.perf_counter()
        v1 = k8s_client.CoreV1Api()
        v1.list_namespace(_request_timeout=5)
        version = None
        try:
            git_version = k8s_client.VersionApi().get_code().git_version
            version = git_version.lstrip("v") if git_version else None
        except Exception:
            pass
        result = {"success": True, "message": "Kubernetes API reachable.", "latency_ms": int((time.perf_counter() - t0) * 1000)}
        if version:
            result["version"] = version
        return result
    except Exception as exc:
        return {"success": False, "message": str(exc)}


def run_collector(collector_key: str, params: dict, query_params: dict) -> dict:
    try:
        from kubernetes import client as k8s_client, config as k8s_config  # type: ignore[import]
    except ImportError:
        raise RuntimeError(
            "Kubernetes collector requires the 'kubernetes' client. Add kubernetes to backend/requirements.txt and rebuild."
        )

    _load_k8s_config(params)

    resource = query_params.get("resource", "pods")
    namespace = query_params.get("namespace", "all")
    mapping = query_params.get("result_mapping", [])

    v1 = k8s_client.CoreV1Api()
    apps_v1 = k8s_client.AppsV1Api()

    if resource == "pods":
        items = (v1.list_pod_for_all_namespaces() if namespace == "all" else v1.list_namespaced_pod(namespace)).items
    elif resource == "nodes":
        items = v1.list_node().items
    elif resource == "deployments":
        items = (apps_v1.list_deployment_for_all_namespaces() if namespace == "all" else apps_v1.list_namespaced_deployment(namespace)).items
    else:
        raise ValueError(f"Unsupported Kubernetes resource: '{resource}'")

    result = {}
    for rule in mapping:
        metric_name = rule.get("metric_name", "")
        aggregate = rule.get("aggregate", "count")
        field = rule.get("field")
        flt = rule.get("filter", {})

        subset = [item for item in items if _matches(item, flt)] if flt else list(items)

        if aggregate == "count":
            result[metric_name] = float(len(subset))
        elif aggregate == "sum" and field:
            result[metric_name] = float(sum(_get_field(item, field) or 0 for item in subset))

    return result


def _load_k8s_config(params: dict) -> None:
    from kubernetes import config as k8s_config  # type: ignore[import]

    kubeconfig_env = params.get("kubeconfig_env")
    kubeconfig_path = os.getenv(str(kubeconfig_env)) if kubeconfig_env else None
    try:
        if kubeconfig_path:
            k8s_config.load_kube_config(config_file=kubeconfig_path)
        else:
            k8s_config.load_incluster_config()
    except Exception:
        try:
            k8s_config.load_kube_config()
        except Exception as exc:
            raise RuntimeError(f"Could not load Kubernetes config: {exc}")


def _matches(item, flt: dict) -> bool:
    for key, expected in flt.items():
        if key == "status.phase":
            if getattr(getattr(item, "status", None), "phase", None) != expected:
                return False
        elif key == "waiting_reason":
            statuses = getattr(getattr(item, "status", None), "container_statuses", None) or []
            if not any(
                getattr(getattr(cs.state, "waiting", None), "reason", None) == expected
                for cs in statuses
            ):
                return False
        elif key == "condition":
            conditions = getattr(getattr(item, "status", None), "conditions", None) or []
            if expected == "pressure" and not any(
                c.type in ("MemoryPressure", "DiskPressure", "PIDPressure") and c.status == "True"
                for c in conditions
            ):
                return False
    return True


def _get_field(item, dotted_path: str):
    obj = item
    for part in dotted_path.split("."):
        obj = getattr(obj, part, None)
        if obj is None:
            return None
    return obj
