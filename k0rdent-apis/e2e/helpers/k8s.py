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


def condition(cr: dict[str, Any], cond_type: str) -> dict[str, Any] | None:
    for c in ((cr.get("status") or {}).get("conditions") or []):
        if c.get("type") == cond_type:
            return c
    return None


def reconcile_ready(cr: dict[str, Any]) -> bool:
    """True when UFO reports ReconcileReady=True for the observed generation.

    Absent is not False: the reconciler adds its finalizer and requeues before
    ever writing the condition (see vpcpeering_controller.go), so a freshly
    created object legitimately has no condition yet.
    """
    cond = condition(cr, "ReconcileReady")
    if not cond or str(cond.get("status")).lower() != "true":
        return False
    generation = (cr.get("metadata") or {}).get("generation")
    observed = (cr.get("status") or {}).get("observedGeneration")
    return generation is None or observed == generation


def ufo_security_group_name(uid: str) -> str:
    """Deterministic UFO SecurityGroup CR name: sg-<uid>."""
    return f"sg-{uid}"


def ufo_vpc_peering_name(uid: str) -> str:
    """Deterministic UFO VpcPeering CR name: vpcpeering-<uid>.

    ResourceName(KindVpcPeering, peering.uid) in k0rdent-apis
    internal/model/compute.go. Note the Vpc CR name is NOT derivable this way —
    it is vpc-<clusterResourceId>-<schemaEntryId> — so read `ufoCrName` off the
    VPC API row instead of composing it here.
    """
    return f"vpcpeering-{uid}"


# Label UFO stamps on the single backend peering object it creates per mutual
# pair (internal/controller/backends/fabric/vpcpeering.go). The object's own name
# is derived from the sorted endpoint pair and hash-truncated past the length
# limit, so match on this label rather than recomputing the name.
LABEL_UFO_VPC_PEERING = "ufo.mirantis.com/vpcpeering"


# Stamped by the k0rdent-apis workflow on every UFO object it applies
# (managedLabels() in activities/shared_types.go). It is the ONLY label there —
# nothing correlates a CR back to its API row — so callers narrow with it and
# then match on spec content.
LABEL_K0RDENT_MANAGED = "app.k0rdent.ai/managed"


def find_ufo_vpc_peering(
    api: client.ApiClient,
    namespace: str,
    *,
    local_cr: str,
    remote_cr: str,
    remote_namespace: str | None = None,
) -> dict[str, Any] | None:
    """The managed UFO VpcPeering in `namespace` joining local_cr -> remote_cr.

    Matched on spec rather than by name: the name is vpcpeering-<peering uid>,
    which the test asserts separately but must not have to compose in order to
    find the object. Direction matters — local and remote are not swapped, so
    the mirror is a different object and will not match.
    """
    for item in list_custom(
        api,
        group="ufo.mirantis.com",
        version="v1alpha1",
        plural="vpcpeerings",
        namespace=namespace,
        label_selector=LABEL_K0RDENT_MANAGED,
    ).get("items", []):
        spec = item.get("spec") or {}
        local = (spec.get("local") or {}).get("name")
        remote = spec.get("remote") or {}
        if local != local_cr or remote.get("name") != remote_cr:
            continue
        # Absent and empty both mean "same namespace as the CR".
        if (remote.get("namespace") or None) != remote_namespace:
            continue
        return item
    return None


def owns(owner_cr: dict[str, Any], obj: dict[str, Any]) -> bool:
    """True when obj carries an ownerReference to owner_cr (matched by uid)."""
    owner_uid = (owner_cr.get("metadata") or {}).get("uid")
    if not owner_uid:
        return False
    for ref in (obj.get("metadata") or {}).get("ownerReferences") or []:
        if ref.get("uid") == owner_uid:
            return True
    return False


def backend_peerings_owned_by(
    api: client.ApiClient,
    owner_crs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Backend peering objects owned by any of the given UFO VpcPeering CRs.

    Owner-reference matching keeps the test out of UFO's canonical-naming
    algorithm (sorted endpoint pair, hash-truncated, placed in the
    lexicographically smaller namespace) — where the object lives and what it is
    called are UFO's business, not the test's. Ownership can legitimately sit on
    either side of a mutual pair, so pass both.
    """
    return [
        item
        for item in list_nico_vpc_peerings(api)
        if any(owns(owner, item) for owner in owner_crs)
    ]


def list_nico_vpc_peerings(
    api: client.ApiClient,
    namespace: str | None = None,
) -> list[dict[str, Any]]:
    """NICo VPCPeering objects, cluster-wide by default.

    The canonical namespace is the lexicographically smaller of the two peered
    Vpc namespaces, which is not necessarily the namespace either side's UFO
    VpcPeering lives in — so cross-namespace callers must not pass one.
    """
    custom = client.CustomObjectsApi(api)
    kwargs = dict(
        group="nico.mirantis.com",
        version="v1alpha1",
        plural="vpcpeerings",
        label_selector=LABEL_UFO_VPC_PEERING,
    )
    try:
        if namespace:
            resp = custom.list_namespaced_custom_object(namespace=namespace, **kwargs)
        else:
            resp = custom.list_cluster_custom_object(**kwargs)
    except ApiException as e:
        if e.status == 404:
            return []
        raise
    return resp.get("items", [])


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
