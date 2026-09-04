"""Kubernetes helpers for asserting materialized CRs."""

from __future__ import annotations

from typing import Any

from kubernetes import client, config
from kubernetes.client.rest import ApiException


def api_client(kubeconfig: str | None = None):
    if kubeconfig:
        config.load_kube_config(config_file=kubeconfig)
    else:
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
    return client.ApiClient()


def project_namespace(project: str) -> str:
    return f"prj-{project}"


def list_custom(
    api: client.ApiClient,
    group: str,
    version: str,
    plural: str,
    namespace: str,
    label_selector: str | None = None,
):
    custom = client.CustomObjectsApi(api)
    return custom.list_namespaced_custom_object(
        group=group,
        version=version,
        namespace=namespace,
        plural=plural,
        label_selector=label_selector,
    )


def get_custom(
    api: client.ApiClient,
    group: str,
    version: str,
    plural: str,
    namespace: str,
    name: str,
) -> dict[str, Any] | None:
    custom = client.CustomObjectsApi(api)
    try:
        return custom.get_namespaced_custom_object(
            group=group,
            version=version,
            namespace=namespace,
            plural=plural,
            name=name,
        )
    except ApiException as e:
        if e.status == 404:
            return None
        raise


def ufo_security_group_name(uid: str) -> str:
    """Deterministic UFO SecurityGroup CR name: sg-<uid>."""
    return f"sg-{uid}"


# Labels stamped on ClusterDeployment by the create workflow (see k0rdent-apis
# internal/model/labels.go). metadata.name is cd-<compressed-uid>, not the slug.
LABEL_API_CLUSTER_SLUG = "k0rdent.mirantis.com/api-cluster-slug"
LABEL_API_CLUSTER_UID = "k0rdent.mirantis.com/api-cluster-uid"


def find_cluster_deployment(
    api: client.ApiClient,
    namespace: str,
    *,
    slug: str | None = None,
    uid: str | None = None,
) -> dict[str, Any] | None:
    """Find the ClusterDeployment for a compute cluster by slug and/or uid label."""
    if slug:
        selector = f"{LABEL_API_CLUSTER_SLUG}={slug}"
    elif uid:
        selector = f"{LABEL_API_CLUSTER_UID}={uid}"
    else:
        raise ValueError("slug or uid required")
    items = list_custom(
        api,
        group="k0rdent.mirantis.com",
        version="v1beta1",
        plural="clusterdeployments",
        namespace=namespace,
        label_selector=selector,
    ).get("items", [])
    if not items:
        return None
    if uid and slug:
        for item in items:
            labels = (item.get("metadata") or {}).get("labels") or {}
            if labels.get(LABEL_API_CLUSTER_UID) == uid:
                return item
    return items[0]


def cluster_deployment_ready(cd: dict[str, Any]) -> bool:
    """True when ClusterDeployment reports Ready=True (condition or status.ready)."""
    status = cd.get("status") or {}
    if status.get("ready") is True:
        return True
    for c in status.get("conditions") or []:
        if c.get("type") == "Ready" and str(c.get("status")).lower() in ("true", "1"):
            return True
    return False
