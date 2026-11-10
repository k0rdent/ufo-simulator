"""Assert that an owner's materialized networking goes with it.

"Owner" is the cluster or instance group, as in the VPC rows'
``ownerKind``/``ownerId``. Its *materialized networking* is what it creates
from its ClusterType's ``networkSchema`` and node pools' ``networkAttachment``:

  * the VPC API rows it owns, and in its project namespace the UFO objects:
  * a ``Vpc`` per ``networkSchema.vpcs[]`` (the VPC row's ``ufoCrName``),
  * a ``Network`` per ``networks[]`` and a ``Subnet`` per ``subnets[]`` (both
    matched by ``spec.vpc``),
  * a ``NetworkBundle`` per node pool, its rendered ``networkAttachment``
    (matched by the ``nb-<rid>-`` name prefix).

Not covered: SubnetAllocations, SubnetPools and SubnetPoolAllocations,
NetworkConfigAllocations, SecurityGroups.

Why: since the move to network templates (k0rdent-apis KNF-460) a ClusterType's
networking is resolved from the templates on every read, and terminate derives
the names of everything it deletes from it (``cluster_deployment_terminate_v2.go``
``teardownPlan``). Upstream names the failure mode: a reader that resolves a
templated type's networking as ``{}`` plans to delete nothing, silently. The
owner's own row still goes, so the "wait for 404" every test already does would
pass. This checks the networking actually went too.

Mock-agnostic: :func:`snapshot` records only the CRs that exist when it runs,
so under MOCK_MODE (where the workflow may apply none) only the API rows are
asserted.

Usage, in a test that deletes its owner::

    net = materialized_networking.snapshot(...)    # once the owner is active
    ... DELETE the owner, wait for its 404 ...
    materialized_networking.await_gone(..., net)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from helpers import k8s, secgroups, wait

if TYPE_CHECKING:
    from helpers.steps import Steps

_UFO_GROUP = "ufo.mirantis.com"
_UFO_VERSION = "v1alpha1"

# (plural, kind) of the UFO objects tracked, in report order.
_KINDS = (
    ("networkbundles", "NetworkBundle"),
    ("subnets", "Subnet"),
    ("networks", "Network"),
    ("vpcs", "Vpc"),
)


@dataclass(frozen=True)
class MaterializedNetworking:
    """What an active owner materialized, recorded before its delete."""

    owner_kind: str
    owner_uid: str
    project: str
    vpc_ids: list[str]
    # plural -> CR names that existed at snapshot time
    crs: dict[str, list[str]] = field(default_factory=dict)

    @property
    def namespace(self) -> str:
        return k8s.project_namespace(self.project)

    def describe(self) -> str:
        counts = ", ".join(
            f"{len(self.crs.get(plural) or [])} {kind}" for plural, kind in _KINDS
        )
        return (
            f"{self.owner_kind} {self.owner_uid} "
            f"({len(self.vpc_ids)} VPC row(s); UFO: {counts})"
        )


def _owner_vpcs(session, api_base: str, region: str, project: str, owner_kind: str, owner_uid: str):
    return secgroups.owner_vpcs(
        session,
        api_base,
        region,
        project,
        owner_kind=owner_kind,
        owner_uid=owner_uid,
        backend=None,
    )


def _list(kube, plural: str, ns: str) -> list[dict[str, Any]]:
    return k8s.list_custom(
        kube, group=_UFO_GROUP, version=_UFO_VERSION, plural=plural, namespace=ns
    ).get("items", [])


def _exists(kube, plural: str, ns: str, name: str) -> bool:
    return (
        k8s.get_custom(
            kube, group=_UFO_GROUP, version=_UFO_VERSION, plural=plural, namespace=ns, name=name
        )
        is not None
    )


def _resource_id(vpc: dict[str, Any]) -> str | None:
    """The owner's resource id in UFO object names, from one VPC row.

    The workflow names every object ``<kind>-<rid>-<schema or pool id>``
    (``model.ResourceName``), so ``ufoCrName`` ``vpc-<rid>-<schemaEntryId>``
    gives ``rid`` back. None when the row does not have that shape.
    """
    name, entry = vpc.get("ufoCrName") or "", vpc.get("schemaEntryId") or ""
    prefix, suffix = "vpc-", f"-{entry}"
    if not entry or not name.startswith(prefix) or not name.endswith(suffix):
        return None
    rid = name[len(prefix) : -len(suffix)]
    return rid or None


def snapshot(
    session,
    api_base: str,
    region: str,
    project: str,
    kube,
    *,
    owner_kind: str,
    owner_uid: str,
) -> MaterializedNetworking:
    """Record the owner's VPC rows and the UFO objects behind them.

    ``owner_kind`` is the VPC row's ``ownerKind`` (``cluster`` or
    ``instance_group``); ``owner_uid`` the owner's API ``uid``. Call once the
    owner is active: every lab ClusterType declares at least one VPC, so an
    active owner with none is itself a failure.

    Networks and Subnets are matched by ``spec.vpc`` against the owner's Vpc
    CRs; NetworkBundles, which name no VPC, by the ``nb-<rid>-`` prefix.
    """
    vpcs = _owner_vpcs(session, api_base, region, project, owner_kind, owner_uid)
    assert vpcs, f"active {owner_kind} {owner_uid} owns no VPCs in project {project}"
    ns = k8s.project_namespace(project)

    vpc_crs = sorted(
        name
        for name in (v.get("ufoCrName") for v in vpcs)
        if name and _exists(kube, "vpcs", ns, name)
    )
    in_vpcs = set(vpc_crs)
    crs: dict[str, list[str]] = {"vpcs": vpc_crs}
    for plural in ("networks", "subnets"):
        crs[plural] = sorted(
            item["metadata"]["name"]
            for item in _list(kube, plural, ns)
            if (item.get("spec") or {}).get("vpc") in in_vpcs
        )
    rids = {rid for rid in (_resource_id(v) for v in vpcs) if rid}
    assert len(rids) <= 1, f"VPC rows of {owner_kind} {owner_uid} disagree on the resource id: {rids}"
    crs["networkbundles"] = sorted(
        item["metadata"]["name"]
        for item in _list(kube, "networkbundles", ns)
        if rids and item["metadata"]["name"].startswith(f"nb-{next(iter(rids))}-")
    )
    return MaterializedNetworking(
        owner_kind=owner_kind,
        owner_uid=owner_uid,
        project=project,
        vpc_ids=sorted(v["id"] for v in vpcs),
        crs=crs,
    )


def remaining(session, api_base: str, region: str, kube, net: MaterializedNetworking) -> list[str]:
    """What of ``net`` still exists; empty once the network is torn down."""
    left = [
        f"VPC row {v.get('id')}"
        for v in _owner_vpcs(session, api_base, region, net.project, net.owner_kind, net.owner_uid)
    ]
    for plural, kind in _KINDS:
        left += [
            f"UFO {kind} {name}"
            for name in net.crs.get(plural) or []
            if _exists(kube, plural, net.namespace, name)
        ]
    return left


def await_gone(
    session,
    api_base: str,
    region: str,
    kube,
    net: MaterializedNetworking,
    *,
    steps: "Steps | None" = None,
    timeout: float = 900,
    interval: float = 10,
) -> None:
    """Wait until every VPC row and recorded CR of ``net`` is gone.

    A wait, not an instant assert: the cluster terminate path does not wait for
    its Vpcs, so they can trail the owner's 404.
    """
    last: list[str] = []

    def _gone():
        last[:] = remaining(session, api_base, region, kube, net)
        return True if not last else None

    try:
        wait.await_predicate(
            _gone,
            timeout=timeout,
            interval=interval,
            desc=f"materialized networking of {net.describe()} torn down",
            steps=steps,
            log_every=2,
        )
    except AssertionError:
        raise AssertionError(
            f"materialized networking of {net.describe()} not torn down within {timeout:.0f}s; "
            f"still present: {', '.join(last)}"
        ) from None
