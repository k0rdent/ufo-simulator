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


# UFO / CAPN labels used when asserting per-machine east-west network config.
LABEL_NETWORK_BUNDLE = "ufo.mirantis.com/networkbundle"
LABEL_NICO_MACHINE_ID = "ufo.mirantis.com/machine-id"
LABEL_CAPI_CLUSTER_NAME = "cluster.x-k8s.io/cluster-name"
LABEL_PROVISIONING_LINK_ATTACHMENT = "ufo.mirantis.com/provisioning-linkattachment"

# Spectrum-X host coordinates, stamped on the ServerNICAttachment from
# Server.status.location. UFO's verity backend reads both to name the
# HGXTenantAssignment (<prefix>-hgx-suNN-hostNN) and skips creating it entirely
# when either is unusable. Note utils.HostLocationLabels writes them as EMPTY
# STRINGS rather than omitting them when the location is unknown, so an
# exists-selector matches attachments that carry nothing useful — read the
# values, don't select on presence.
LABEL_SU_ID = "ufo.mirantis.com/su-id"
LABEL_HOST_ID = "ufo.mirantis.com/host-id"

# Netris LinkAttachment.status.status after the VNet apply succeeds
# (controllers/linkattachment_translations.go — not the CR message "Success").
LINK_ATTACHMENT_STATUS_APPLIED = "Applied"


def list_capi_machines(
    api: client.ApiClient,
    namespace: str,
    *,
    cluster_name: str,
) -> list[dict[str, Any]]:
    """CAPI Machines belonging to ``cluster_name`` (ClusterDeployment metadata.name)."""
    return list_custom(
        api,
        group="cluster.x-k8s.io",
        version="v1beta1",
        plural="machines",
        namespace=namespace,
        label_selector=f"{LABEL_CAPI_CLUSTER_NAME}={cluster_name}",
    ).get("items", [])


def list_ufo_nico_network_configs(
    api: client.ApiClient,
    namespace: str,
    *,
    label_selector: str | None = None,
) -> list[dict[str, Any]]:
    """UFO NicoNetworkConfig objects (rendered networkv2 lives here)."""
    return list_custom(
        api,
        group="ufo.mirantis.com",
        version="v1alpha1",
        plural="niconetworkconfigs",
        namespace=namespace,
        label_selector=label_selector,
    ).get("items", [])


def list_servernicattachments(
    api: client.ApiClient,
    namespace: str,
    *,
    label_selector: str | None = None,
) -> list[dict[str, Any]]:
    return list_custom(
        api,
        group="ufo.mirantis.com",
        version="v1alpha1",
        plural="servernicattachments",
        namespace=namespace,
        label_selector=label_selector,
    ).get("items", [])


def list_link_attachments(
    api: client.ApiClient,
    namespace: str,
    *,
    label_selector: str | None = None,
) -> list[dict[str, Any]]:
    """Netris LinkAttachment CRs (k8s.netris.ai)."""
    return list_custom(
        api,
        group="k8s.netris.ai",
        version="v1alpha1",
        plural="linkattachments",
        namespace=namespace,
        label_selector=label_selector,
    ).get("items", [])


def link_attachment_applied(la: dict[str, Any]) -> bool:
    """True when a LinkAttachment has finished applying successfully."""
    status = ((la.get("status") or {}).get("status") or "").strip()
    return status == LINK_ATTACHMENT_STATUS_APPLIED


def is_provisioning_link_attachment(la: dict[str, Any]) -> bool:
    labels = (la.get("metadata") or {}).get("labels") or {}
    return bool(labels.get(LABEL_PROVISIONING_LINK_ATTACHMENT))


def list_hgx_tenant_assignments(
    api: client.ApiClient,
    namespace: str,
    *,
    label_selector: str | None = None,
) -> list[dict[str, Any]]:
    """Verity HGXTenantAssignment CRs — the Spectrum-X east-west attachment.

    UFO's verity backend creates exactly one per host when the fabric type is
    Spectrum-X, owned by the ServerNICAttachment. Readiness is the usual UFO
    shape, so use reconcile_ready(): verity-operator sets ReconcileReady=True
    only after it has PATCHed the switchpoint tenant against the live Verity
    API. These objects carry no labels, so the only join is the owner ref.
    """
    return list_custom(
        api,
        group="verity.mirantis.com",
        version="v1alpha1",
        plural="hgxtenantassignments",
        namespace=namespace,
        label_selector=label_selector,
    ).get("items", [])


def list_p2ps(
    api: client.ApiClient,
    namespace: str,
    *,
    label_selector: str | None = None,
) -> list[dict[str, Any]]:
    """UFO P2P CRs: one per (subnet, link), recording both ends of a /31.

    spec.host.address and spec.switch.address are the two halves. P2P has an
    empty status struct, so it is a content assertion only, never a readiness
    signal. On the NICo path these are owned by a NicoNetworkConfigAllocation
    (the Metal3 path labels them instead, which is why there is no selector
    that works for both).
    """
    return list_custom(
        api,
        group="ufo.mirantis.com",
        version="v1alpha1",
        plural="p2ps",
        namespace=namespace,
        label_selector=label_selector,
    ).get("items", [])


def list_nico_network_config_allocations(
    api: client.ApiClient,
    namespace: str,
    *,
    label_selector: str | None = None,
) -> list[dict[str, Any]]:
    """UFO NicoNetworkConfigAllocation CRs.

    The join between a machine and its P2Ps: spec.machineId and
    spec.networkBundleName mirror the NicoNetworkConfig labels, and both are
    immutable, so the correlation cannot drift mid-test.
    """
    return list_custom(
        api,
        group="ufo.mirantis.com",
        version="v1alpha1",
        plural="niconetworkconfigallocations",
        namespace=namespace,
        label_selector=label_selector,
    ).get("items", [])


def nico_network_config_for_machine(
    api: client.ApiClient,
    namespace: str,
    machine: dict[str, Any],
) -> dict[str, Any] | None:
    """NicoNetworkConfig for a CAPI Machine (same name as its NicoMachine infra)."""
    ref = (machine.get("spec") or {}).get("infrastructureRef") or {}
    name = ref.get("name") or (machine.get("metadata") or {}).get("name")
    if not name:
        return None
    return get_custom(
        api,
        group="ufo.mirantis.com",
        version="v1alpha1",
        plural="niconetworkconfigs",
        namespace=namespace,
        name=name,
    )


def ew_ethernets(networkv2: dict[str, Any] | None) -> dict[str, Any]:
    """Ethernet stanzas whose keys are east-west rail NICs (``eth-ew*``)."""
    if not networkv2:
        return {}
    ethernets = networkv2.get("ethernets") or {}
    return {
        name: eth
        for name, eth in ethernets.items()
        if isinstance(name, str) and name.startswith("eth-ew")
    }
