"""VPC peering: the API-to-CR seam, plus cluster-VPC <-> instance-group-VPC.

MOCK BOUNDARY. These tests require MOCK_MODE **off**
(`lab-inject.sh mock off`). Under mock, VPCPeeringCreate returns before
`vpcPeeringPlan` and before `ApplyUFOVpcPeering`, so no UFO VpcPeering CR is
ever applied and every CR assertion here fails. That is precisely the gap they
exist to close: the upstream k0rdent-apis peering suite is thorough on the
API/DB side but runs under mock, so nothing anywhere has seen a real CR
produced by a REST call, or a real backend peering object.

Two things about peering shape the whole file:

  * A one-sided peering programs NO fabric, and that is not an error. Every
    backend looks for a mirrored CR with the endpoints swapped before doing
    anything, so `state=active` on one side means "this side's CR was applied",
    not "traffic flows". Each test therefore asserts the backend object is
    ABSENT with one side declared and PRESENT once the mirror exists.
  * Endpoints cannot be created directly — there is no tenant create for a VPC;
    they are materialized from a cluster's or instance group's bound
    ClusterType.networkSchema. So the endpoints here come from provisioning a
    cluster and an instance group, which also makes these the only tests
    anywhere that peer across owner kinds (`ownerKind` is never consulted in
    the peering create path).

Teardown order differs by test, and that is deliberate:

  * Same-project tests remove the PEERINGS first, then the owners. That is the
    only safe order there. While two VPCs in one namespace are mutually peered,
    deleting an owner first leaves the survivor's CR naming the dead VPC in the
    same namespace, and UFO's Vpc finalizer counts a same-namespace reference
    unconditionally — on either leg — so the VPC is pinned in Terminating
    indefinitely. (UFO `vpc_controller.go`: the mutual-counterpart escape hatch
    is guarded by `peering.Namespace != vpcNamespace`.) A fix is expected;
    until then the same-project owner-teardown case is deliberately not tested.
  * The cross-org test deletes an OWNER with both halves still declared, which
    is what a tenant actually does. There the survivor is foreign, loses its
    counterpart, and releases correctly — so the real contract is assertable.

Lab prerequisites beyond the other scenarios: TWO available `nico-lab` servers,
because a cluster and an instance group are alive at the same time.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from conftest import (
    auth_configured,
    ensure_global_prereqs,
    load_scenario_template,
    load_template,
)
from helpers import api, k8s, wait
from helpers.names import resource_id, stamp_id
from helpers.steps import Steps

# `smoke` is applied per-test, not here: the cross-org test provisions a second
# owner in a second project and must stay out of the default `-m smoke` run.
pytestmark = [pytest.mark.peering]

_SCENARIO = "vpc_peering"

# The nico VPC both global cluster types declare. Selected by schemaEntryId
# rather than by filtering on backend: both types also declare a verity VPC,
# and an entry id stays unambiguous even if a type grows a second nico VPC.
_NICO_ENTRY = "vpc-nico"


# --------------------------------------------------------------------------
# provisioning
# --------------------------------------------------------------------------


def _await_absent(session, url: str, *, desc: str, log: Steps, timeout: float) -> None:
    wait.await_api_absent(
        lambda: None if api.get(session, url).status_code == 404 else True,
        timeout=timeout,
        interval=15,
        desc=desc,
        steps=log,
        log_every=2,
    )


def _post_fresh(session, collection_url: str, body: dict[str, Any], *, log: Steps) -> str:
    """POST a resource, deleting a leftover of the same id first. Does NOT wait.

    Returning before the resource is active is deliberate: the caller POSTs
    everything it needs and then awaits them together, so provisioning overlaps.
    """
    item_url = f"{collection_url}/{body['id']}"
    existing = api.get(session, item_url)
    if existing.status_code == 200:
        log.info(f"leftover {body['id']} present — deleting")
        api.delete(session, item_url)
        _await_absent(
            session,
            item_url,
            desc=f"{body['id']} gone before recreate",
            log=log,
            timeout=1800,
        )
    elif existing.status_code != 404:
        existing.raise_for_status()

    created = session.post(collection_url, json=body, timeout=60)
    assert created.status_code in (200, 201, 202), created.text
    return item_url


def _post_cluster(
    session, api_base, region, project: str, cluster_id: str, *, log: Steps
) -> str:
    body = stamp_id(load_template("hcp_cluster/cluster.yaml"), cluster_id)
    clusters_url = api.region_url(api_base, region, "compute/clusters", project=project)
    log.step(f"POST cluster {cluster_id} in project {project}")
    url = _post_fresh(session, clusters_url, body, log=log)
    log.ok("create accepted")
    return url


def _post_instance_group(
    session, api_base, region, project: str, ig_id: str, *, log: Steps
) -> str:
    body = stamp_id(load_template("instance_group/instance-group.yaml"), ig_id)
    groups_url = api.region_url(
        api_base, region, "compute/instance-groups", project=project
    )
    log.step(f"POST instance group {ig_id} in project {project}")
    url = _post_fresh(session, groups_url, body, log=log)
    log.ok("create accepted")
    return url


def _delete_owner(session, url: str, what: str, *, log: Steps) -> None:
    log.step(f"DELETE {what}")
    deleted = api.delete(session, url)
    assert deleted.status_code in (202, 204, 404), deleted.text
    _await_absent(session, url, desc=f"{what} deleted", log=log, timeout=1800)


# --------------------------------------------------------------------------
# endpoints
# --------------------------------------------------------------------------


def _owned_vpc(
    session,
    api_base,
    region,
    project: str,
    *,
    owner_kind: str,
    owner_uid: str,
    entry: str = _NICO_ENTRY,
) -> dict[str, Any]:
    """The VPC this owner materialized from the named networkSchema entry."""
    vpcs_url = api.region_url(api_base, region, "networking/vpcs", project=project)
    matches = [
        v
        for v in api.list_items(session, vpcs_url)
        if v.get("ownerKind") == owner_kind
        and v.get("ownerId") == owner_uid
        and v.get("schemaEntryId") == entry
    ]
    assert len(matches) == 1, (
        f"want exactly one {entry!r} vpc owned by {owner_kind} {owner_uid}, "
        f"got {[v.get('id') for v in matches]!r}"
    )
    return matches[0]


def _await_vpc_active(session, api_base, region, project, vpc_id, *, log: Steps) -> dict:
    vpc_url = api.region_url(
        api_base, region, f"networking/vpcs/{vpc_id}", project=project
    )
    return wait.await_api_state(
        lambda: api.get_json(session, vpc_url),
        "active",
        what=f"vpc {vpc_id}",
        timeout=900,
        interval=10,
        steps=log,
        log_every=2,
    )


# --------------------------------------------------------------------------
# peerings
# --------------------------------------------------------------------------


def _peerings_url(api_base, region, project: str, vpc_id: str) -> str:
    return api.region_url(
        api_base, region, f"networking/vpcs/{vpc_id}/peerings", project=project
    )


def _create_peering(
    session,
    api_base,
    region,
    *,
    local_project: str,
    local_vpc_id: str,
    remote_project: str,
    remote_vpc_id: str,
    peering_id: str,
    log: Steps,
) -> tuple[dict[str, Any], str]:
    """Declare ONE SIDE of a peering. Returns (row, item url).

    remoteVpc is injected rather than templated: it is a URI path embedding the
    live region and project. The body is strict-decoded server-side, so nothing
    read-only may be added to it.
    """
    body = stamp_id(load_scenario_template(_SCENARIO, "peering.yaml"), peering_id)
    body["remoteVpc"] = api.vpc_uri(region, remote_project, remote_vpc_id)

    collection = _peerings_url(api_base, region, local_project, local_vpc_id)
    log.step(f"POST peering {peering_id}: {local_vpc_id} -> {remote_vpc_id}")
    created = session.post(collection, json=body, timeout=60)
    assert created.status_code == 201, created.text
    row = created.json()
    log.ok(f"201, state={row.get('state')!r}")
    return row, f"{collection}/{peering_id}"


def _await_peering_active(session, url: str, peering_id: str, *, log: Steps) -> dict:
    return wait.await_api_state(
        lambda: api.get_json(session, url),
        "active",
        what=f"peering {peering_id}",
        timeout=900,
        interval=10,
        steps=log,
        log_every=2,
    )


def _settled_state(pred, peering_id: str, *, log: Steps) -> str:
    row = wait.await_predicate(
        pred,
        timeout=900,
        interval=10,
        desc=f"peering {peering_id} settled (active/failed) before delete",
        steps=log,
        log_every=2,
    )
    return str(row.get("state"))


def _delete_peering(session, url: str, peering_id: str, *, log: Steps) -> None:
    """Settle, delete, then wait for the tombstone.

    Both waits are required, not tidiness. DELETE is a compare-and-swap over
    state IN ('active','failed'), so against a row still `creating` it answers
    409 and the row keeps its direction. And the direction is held until the
    row is tombstoned, not until the 204, because uq_vpc_peering_direction is
    partial on deleted_at IS NULL.
    """
    log.step(f"DELETE peering {peering_id}")

    def _settled():
        resp = api.get(session, url)
        if resp.status_code == 404:
            return {"state": "gone"}
        resp.raise_for_status()
        row = resp.json()
        # 'failed' counts as settled: DELETE accepts active OR failed, and a
        # failed row still holds its direction until tombstoned.
        return row if row.get("state") in ("active", "failed") else None

    if _settled_state(_settled, peering_id, log=log) == "gone":
        return

    deleted = api.delete(session, url)
    assert deleted.status_code in (204, 404), deleted.text
    _await_absent(
        session, url, desc=f"peering {peering_id} deleted", log=log, timeout=900
    )


# --------------------------------------------------------------------------
# CR assertions
# --------------------------------------------------------------------------


def _await_peering_cr(
    kube,
    namespace: str,
    *,
    local_cr: str,
    remote_cr: str,
    remote_namespace: str | None,
    log: Steps,
) -> dict[str, Any]:
    """The UFO VpcPeering CR for one side, found by spec rather than by name."""

    def _found():
        cr = k8s.find_ufo_vpc_peering(
            kube,
            namespace,
            local_cr=local_cr,
            remote_cr=remote_cr,
            remote_namespace=remote_namespace,
        )
        return cr if cr and k8s.reconcile_ready(cr) else None

    return wait.await_predicate(
        _found,
        timeout=300,
        interval=5,
        desc=f"UFO VpcPeering {local_cr} -> {remote_cr} in {namespace} ReconcileReady",
        steps=log,
        log_every=2,
    )


def _await_peering_cr_absent(
    kube,
    namespace: str,
    *,
    local_cr: str,
    remote_cr: str,
    remote_namespace: str | None,
    log: Steps,
) -> None:
    """Wait until the UFO VpcPeering CR for one side is gone.

    Matched on spec, like the positive lookup — the CR carries a finalizer that
    is held until its backend cleanup finishes, so "deleted" here means really
    gone, not merely marked for deletion.
    """

    def _gone():
        return (
            True
            if k8s.find_ufo_vpc_peering(
                kube,
                namespace,
                local_cr=local_cr,
                remote_cr=remote_cr,
                remote_namespace=remote_namespace,
            )
            is None
            else None
        )

    wait.await_predicate(
        _gone,
        timeout=600,
        interval=10,
        desc=f"UFO VpcPeering {local_cr} -> {remote_cr} in {namespace} gone",
        steps=log,
        log_every=2,
    )


def _assert_peering_cr(cr: dict[str, Any], row: dict[str, Any], *, remote_ns: str | None) -> None:
    spec = cr.get("spec") or {}
    remote = spec.get("remote") or {}
    assert (remote.get("namespace") or None) == remote_ns, (
        f"spec.remote.namespace={remote.get('namespace')!r}, want {remote_ns!r}"
    )
    # Nothing else pins the derived name, so assert it once here — but never
    # use it to locate the object.
    assert cr["metadata"]["name"] == k8s.ufo_vpc_peering_name(row["uid"]), (
        f"CR name {cr['metadata']['name']!r} != vpcpeering-{row['uid']}"
    )


def _await_backend_peering(kube, owners: list[dict], *, log: Steps) -> dict[str, Any]:
    def _found():
        found = k8s.backend_peerings_owned_by(kube, owners)
        return found if found else None

    found = wait.await_predicate(
        _found,
        timeout=600,
        interval=10,
        desc="backend VPCPeering owned by the mutual pair",
        steps=log,
        log_every=2,
    )
    assert len(found) == 1, (
        "a mutual pair must collapse onto exactly one backend peering, got "
        f"{[i['metadata']['name'] for i in found]!r}"
    )
    return found[0]


def _await_no_backend_peering(kube, owners: list[dict], *, log: Steps) -> None:
    def _gone():
        return True if not k8s.backend_peerings_owned_by(kube, owners) else None

    wait.await_predicate(
        _gone,
        timeout=600,
        interval=10,
        desc="backend VPCPeering removed",
        steps=log,
        log_every=2,
    )


# --------------------------------------------------------------------------
# the handshake, shared by both tests
# --------------------------------------------------------------------------


class _Handshake:
    """Both declared sides of one peering, filled in as they are created.

    `fwd` is the side whose local VPC is `local_vpc`; `rev` is its mirror.
    Mutable and passed in by the caller rather than returned, so a `finally`
    can clean up whichever sides exist when a run fails partway through.
    """

    # Empty string rather than None for the ids/urls: every caller guards on
    # truthiness anyway, and it keeps them plain `str` for the helpers below.
    def __init__(self) -> None:
        self.fwd_id: str = ""
        self.rev_id: str = ""
        self.fwd_url: str = ""
        self.rev_url: str = ""
        self.crs: list[dict[str, Any]] = []


def _declare_both_sides(
    session,
    api_base,
    region,
    kube,
    *,
    hs: _Handshake,
    log: Steps,
    run_id: str,
    test_name: str,
    local_project: str,
    local_vpc: dict[str, Any],
    remote_project: str,
    remote_vpc: dict[str, Any],
) -> None:
    """Declare both sides and assert the CRs and the single backend object.

    Deliberately has no teardown of its own: how the pair is torn down is the
    thing the callers differ on, and in one of them it is the thing under test.

    Cross-project only in that the two projects may differ: when they do, the
    CRs carry spec.remote.namespace; when they do not, it must be absent.
    """
    cross_project = local_project != remote_project
    local_ns = k8s.project_namespace(local_project)
    remote_ns = k8s.project_namespace(remote_project)

    hs.fwd_id = resource_id(test_name, "fwd", run_id=run_id)
    hs.rev_id = resource_id(test_name, "rev", run_id=run_id)

    fwd_row, hs.fwd_url = _create_peering(
        session,
        api_base,
        region,
        local_project=local_project,
        local_vpc_id=local_vpc["id"],
        remote_project=remote_project,
        remote_vpc_id=remote_vpc["id"],
        peering_id=hs.fwd_id,
        log=log,
    )
    fwd_row = _await_peering_active(session, hs.fwd_url, hs.fwd_id, log=log)

    log.step(f"assert UFO VpcPeering CR for {hs.fwd_id} in {local_ns}")
    fwd_cr = _await_peering_cr(
        kube,
        local_ns,
        local_cr=local_vpc["ufoCrName"],
        remote_cr=remote_vpc["ufoCrName"],
        remote_namespace=remote_ns if cross_project else None,
        log=log,
    )
    _assert_peering_cr(fwd_cr, fwd_row, remote_ns=remote_ns if cross_project else None)
    hs.crs.append(fwd_cr)
    log.ok(f"CR {fwd_cr['metadata']['name']} ReconcileReady")

    # Ordered, not raced: UFO adds its finalizer, calls the backend, and only
    # then writes ReconcileReady=True. So by the time the CR above reports
    # Ready, the backend has already run and skipped for want of a mirror. A
    # backend object here would be a real defect.
    log.step("assert NO backend peering yet (one side programs nothing)")
    assert not k8s.backend_peerings_owned_by(kube, hs.crs), (
        "a one-sided peering must not program the fabric"
    )
    log.ok("none, as expected")

    rev_row, hs.rev_url = _create_peering(
        session,
        api_base,
        region,
        local_project=remote_project,
        local_vpc_id=remote_vpc["id"],
        remote_project=local_project,
        remote_vpc_id=local_vpc["id"],
        peering_id=hs.rev_id,
        log=log,
    )
    rev_row = _await_peering_active(session, hs.rev_url, hs.rev_id, log=log)
    assert rev_row["uid"] != fwd_row["uid"], "the two sides must be distinct resources"

    log.step(f"assert UFO VpcPeering CR for {hs.rev_id} in {remote_ns}")
    rev_cr = _await_peering_cr(
        kube,
        remote_ns,
        local_cr=remote_vpc["ufoCrName"],
        remote_cr=local_vpc["ufoCrName"],
        remote_namespace=local_ns if cross_project else None,
        log=log,
    )
    _assert_peering_cr(rev_cr, rev_row, remote_ns=local_ns if cross_project else None)
    hs.crs.append(rev_cr)
    log.ok(f"CR {rev_cr['metadata']['name']} ReconcileReady")

    log.step("assert the mutual pair collapsed onto ONE backend peering")
    backend = _await_backend_peering(kube, hs.crs, log=log)
    log.ok(
        f"{backend['metadata']['namespace']}/{backend['metadata']['name']}"
        f" status.id={(backend.get('status') or {}).get('id')!r}"
    )

    log.step("assert each side is listed only under its own VPC")
    under_local = {
        p["id"]
        for p in api.list_items(
            session, _peerings_url(api_base, region, local_project, local_vpc["id"])
        )
    }
    under_remote = {
        p["id"]
        for p in api.list_items(
            session, _peerings_url(api_base, region, remote_project, remote_vpc["id"])
        )
    }
    assert hs.fwd_id in under_local, f"{hs.fwd_id} missing under its own vpc"
    assert hs.rev_id in under_remote, f"{hs.rev_id} missing under its own vpc"
    assert hs.rev_id not in under_local, f"{hs.rev_id} must not be listed under the local vpc"
    assert hs.fwd_id not in under_remote, f"{hs.fwd_id} must not be listed under the remote vpc"
    log.ok()


def _run_handshake(
    session,
    api_base,
    region,
    kube,
    *,
    log: Steps,
    run_id: str,
    test_name: str,
    local_project: str,
    local_vpc: dict[str, Any],
    remote_project: str,
    remote_vpc: dict[str, Any],
) -> None:
    """Declare both sides, assert, then remove the PEERINGS before the owners.

    That order is load-bearing, not tidiness: while two VPCs in one namespace
    are mutually peered, deleting an owner first leaves the survivor's CR
    naming the dead VPC in the same namespace, which pins it in Terminating
    indefinitely. The cross-org teardown test drives the other order, where the
    survivor is foreign and releases correctly.
    """
    hs = _Handshake()
    try:
        _declare_both_sides(
            session,
            api_base,
            region,
            kube,
            hs=hs,
            log=log,
            run_id=run_id,
            test_name=test_name,
            local_project=local_project,
            local_vpc=local_vpc,
            remote_project=remote_project,
            remote_vpc=remote_vpc,
        )
    finally:
        # Reverse first: removing either half withdraws the fabric peering, and
        # this order lets us assert that the backend object actually goes.
        if hs.rev_url:
            _delete_peering(session, hs.rev_url, hs.rev_id, log=log)
            if len(hs.crs) == 2:
                log.step("assert the backend peering went with the mirror")
                _await_no_backend_peering(kube, hs.crs, log=log)
                log.ok()
        if hs.fwd_url:
            _delete_peering(session, hs.fwd_url, hs.fwd_id, log=log)


def _run_owner_teardown(
    session,
    api_base,
    region,
    kube,
    *,
    log: Steps,
    run_id: str,
    test_name: str,
    local_project: str,
    local_vpc: dict[str, Any],
    remote_project: str,
    remote_vpc: dict[str, Any],
    local_owner_url: str,
    local_owner_what: str,
    remote_owner_url: str,
    remote_owner_what: str,
) -> None:
    """Delete BOTH owners in turn, with the peerings never deleted by hand.

    Two halves, covering the two different teardown implementations:

      1. Delete the remote owner (the instance group, via
         `providers/ufo/teardown_network.go`). Only its own side may go — row
         tombstoned and UFO CR deleted by its terminate workflow — while the
         counterpart's row and CR are left deliberately in place, dangling at a
         VPC that no longer exists, with no signal to that tenant.
      2. Delete the local owner (the cluster, via
         `cluster_deployment_terminate_v2.go`). The surviving peering must now
         go **with it**, without the test deleting it — which is what proves the
         cleanup is the owner's, not the test's. The two paths differ: the
         instance-group one waits for its Vpcs, the cluster one does not.

    Nothing else tests either: upstream's equivalent runs under MOCK_MODE and
    disclaims proving that any CR was applied or removed.

    Cross-project only. Same-project would wedge the deleted owner's Vpc in
    Terminating, because the survivor's CR sits in the same namespace and so is
    counted unconditionally by UFO's finalizer check.
    """
    local_ns = k8s.project_namespace(local_project)
    remote_ns = k8s.project_namespace(remote_project)

    hs = _Handshake()
    try:
        _declare_both_sides(
            session,
            api_base,
            region,
            kube,
            hs=hs,
            log=log,
            run_id=run_id,
            test_name=test_name,
            local_project=local_project,
            local_vpc=local_vpc,
            remote_project=remote_project,
            remote_vpc=remote_vpc,
        )

        log.step(f"DELETE {remote_owner_what} with BOTH peering sides still live")
        _delete_owner(session, remote_owner_url, remote_owner_what, log=log)
        log.ok("owner gone")

        log.step(f"assert the torn-down owner's side ({hs.rev_id}) is gone")
        _await_absent(
            session,
            hs.rev_url,
            desc=f"peering {hs.rev_id} removed with its owner",
            log=log,
            timeout=900,
        )
        _await_peering_cr_absent(
            kube,
            remote_ns,
            local_cr=remote_vpc["ufoCrName"],
            remote_cr=local_vpc["ufoCrName"],
            remote_namespace=local_ns,
            log=log,
        )
        hs.rev_url = ""  # already gone; keep the finally from re-deleting it
        log.ok("row and CR both removed by the owner's teardown")

        log.step(f"assert the counterpart ({hs.fwd_id}) SURVIVES, dangling")
        survivor = api.get(session, hs.fwd_url)
        assert survivor.status_code == 200, (
            f"the counterpart must outlive the other owner, got {survivor.status_code}: "
            f"{survivor.text}"
        )
        assert (
            k8s.find_ufo_vpc_peering(
                kube,
                local_ns,
                local_cr=local_vpc["ufoCrName"],
                remote_cr=remote_vpc["ufoCrName"],
                remote_namespace=remote_ns,
            )
            is not None
        ), "the counterpart's UFO CR must be left in place"
        log.ok(f"row state={survivor.json().get('state')!r}, CR still present")

        log.step("assert the fabric peering went with the torn-down side")
        _await_no_backend_peering(kube, hs.crs, log=log)
        log.ok()

        # Second half: the surviving peering is still live and still declared.
        # Deleting its owner — never the peering itself — is what proves the
        # cleanup belongs to the owner's terminate workflow. This also exercises
        # the cluster path, which unlike the instance-group one does not wait
        # for its Vpcs.
        log.step(f"DELETE {local_owner_what} with its peering still declared")
        _delete_owner(session, local_owner_url, local_owner_what, log=log)
        log.ok("owner gone")

        log.step(f"assert the surviving side ({hs.fwd_id}) went with its owner")
        _await_absent(
            session,
            hs.fwd_url,
            desc=f"peering {hs.fwd_id} removed with its owner",
            log=log,
            timeout=900,
        )
        _await_peering_cr_absent(
            kube,
            local_ns,
            local_cr=local_vpc["ufoCrName"],
            remote_cr=remote_vpc["ufoCrName"],
            remote_namespace=remote_ns,
            log=log,
        )
        hs.fwd_url = ""
        log.ok("row and CR both removed by the owner's teardown")
    finally:
        # Safety net only: on the happy path both sides are already gone, each
        # removed by its own owner's teardown. These run when an assertion
        # failed before the owners were deleted.
        if hs.fwd_url:
            _delete_peering(session, hs.fwd_url, hs.fwd_id, log=log)
        if hs.rev_url:
            _delete_peering(session, hs.rev_url, hs.rev_id, log=log)


# --------------------------------------------------------------------------
# the scenario, shared by both tests
# --------------------------------------------------------------------------


def _peer_cluster_with_instance_group(
    session,
    api_base,
    region,
    *,
    log: Steps,
    run_id: str,
    test_name: str,
    cluster_project: str,
    ig_project: str,
    owner_teardown: bool = False,
) -> None:
    """Peer a cluster's nico VPC with an instance group's, and tear both down.

    Every test is this same scenario; two things vary. Which project the
    instance group goes in: same as the cluster gives the plain handshake
    (spec.remote.namespace absent), a project in another org gives the
    cross-org case (spec.remote.namespace set). And `owner_teardown`, which
    picks the teardown ORDER — the thing under test in one of them:

      * False — remove the peerings, then the owners. The only safe order
        same-project, and what the handshake tests use.
      * True  — remove the instance group with both halves still declared, and
        assert only its own side goes. Cross-project only.

    Everything else — resolving the endpoints, declaring both halves, the CR
    and backend-object assertions — is identical, so it lives here rather than
    being written three times.

    Resources are created inline and cleaned up in `finally`, matching the rest
    of the suite; nothing is shared across tests.
    """
    if owner_teardown and cluster_project == ig_project:
        raise AssertionError(
            "owner_teardown is cross-project only: same-project would wedge the "
            "deleted owner's Vpc in Terminating"
        )
    log.step("ensure global prereqs (address-pools + cluster-types; never deleted)")
    ensure_global_prereqs(session, api_base, region)
    log.ok()

    kube = k8s.api_client(os.environ.get("KUBECONFIG"))

    cluster_id = resource_id(test_name, "cluster", run_id=run_id)
    ig_id = resource_id(test_name, "ig", run_id=run_id)
    cluster_url = ig_url = None

    try:
        # POST both before awaiting either, so they provision concurrently and
        # this costs max(cluster, ig) rather than the sum. Creates are async —
        # POST returns long before anything is provisioned — so no threads are
        # needed, only deferred waits.
        cluster_url = _post_cluster(
            session, api_base, region, cluster_project, cluster_id, log=log
        )
        ig_url = _post_instance_group(
            session, api_base, region, ig_project, ig_id, log=log
        )

        log.step("wait for cluster AND instance group to reach active (concurrently)")
        cluster_key = f"cluster {cluster_id}"
        ig_key = f"instance group {ig_id}"
        settled = wait.await_api_states(
            {
                cluster_key: lambda: api.get_json(session, cluster_url),
                ig_key: lambda: api.get_json(session, ig_url),
            },
            "active",
            timeout=1800,
            interval=15,
            steps=log,
            log_every=2,
        )
        cluster_uid = settled[cluster_key]["uid"]
        ig_uid = settled[ig_key]["uid"]
        log.ok(f"cluster uid={cluster_uid} ig uid={ig_uid}")

        log.step(f"resolve the {_NICO_ENTRY!r} VPC on each side")
        cluster_vpc = _await_vpc_active(
            session,
            api_base,
            region,
            cluster_project,
            _owned_vpc(
                session,
                api_base,
                region,
                cluster_project,
                owner_kind="cluster",
                owner_uid=cluster_uid,
            )["id"],
            log=log,
        )
        ig_vpc = _await_vpc_active(
            session,
            api_base,
            region,
            ig_project,
            _owned_vpc(
                session,
                api_base,
                region,
                ig_project,
                owner_kind="instance_group",
                owner_uid=ig_uid,
            )["id"],
            log=log,
        )
        # A peering must join two VPCs on the same backend — UFO's webhook
        # denies anything else — so this is a precondition, not a nicety.
        backends = {
            (cluster_vpc.get("backend") or "").lower(),
            (ig_vpc.get("backend") or "").lower(),
        }
        assert backends == {"nico"}, (
            f"both endpoints must be on the nico backend, got {backends!r}"
        )
        log.ok(
            f"{cluster_project}/{cluster_vpc['id']} ({cluster_vpc['ufoCrName']}) <-> "
            f"{ig_project}/{ig_vpc['id']} ({ig_vpc['ufoCrName']})"
        )

        scenario = _run_owner_teardown if owner_teardown else _run_handshake
        extra = (
            {
                "local_owner_url": cluster_url,
                "local_owner_what": f"cluster {cluster_id}",
                "remote_owner_url": ig_url,
                "remote_owner_what": f"instance group {ig_id}",
            }
            if owner_teardown
            else {}
        )
        scenario(
            session,
            api_base,
            region,
            kube,
            log=log,
            run_id=run_id,
            test_name=test_name,
            local_project=cluster_project,
            local_vpc=cluster_vpc,
            remote_project=ig_project,
            remote_vpc=ig_vpc,
            **extra,
        )
    finally:
        # Instance group first: it is the cheaper of the two to re-create, and
        # ordering keeps at most one owner in teardown at a time.
        if ig_url:
            _delete_owner(session, ig_url, f"instance group {ig_id}", log=log)
        if cluster_url:
            _delete_owner(session, cluster_url, f"cluster {cluster_id}", log=log)


# --------------------------------------------------------------------------
# tests
# --------------------------------------------------------------------------


def _require_peer_project(session, api_base, region, project: str, peer_project: str) -> None:
    """Skip unless a second, distinct project is reachable for the cross-org tests."""
    if peer_project == project:
        pytest.skip("E2E_PEER_PROJECT must name a project other than PROJECT")
    probe = api.get(
        session,
        api.region_url(api_base, region, "compute/instance-groups", project=peer_project),
    )
    if probe.status_code == 404:
        pytest.skip(f"peer project {peer_project!r} not present on this lab")
    probe.raise_for_status()


@pytest.mark.smoke
@pytest.mark.skipif(not auth_configured(), reason="API_BASE required")
def test_vpc_peering_cluster_to_instance_group(
    session, api_base, region, project, run_id, request
):
    """Peer a cluster-owned VPC with an instance-group-owned VPC, same project.

    The cross-ownerKind pairing is permitted by every layer but exercised
    nowhere else: upstream peers cluster<->cluster, the compute-infrastructure
    suite peers instance-group<->instance-group.
    """
    log = Steps("VPC peering: cluster <-> instance group")
    log.info(f"run_id={run_id} project={project}")

    _peer_cluster_with_instance_group(
        session,
        api_base,
        region,
        log=log,
        run_id=run_id,
        test_name=request.node.name,
        cluster_project=project,
        ig_project=project,
    )
    log.done()


@pytest.mark.crossorg
@pytest.mark.skipif(not auth_configured(), reason="API_BASE required")
def test_vpc_peering_cross_org(
    session, api_base, region, project, peer_project, run_id, request
):
    """Peer across two orgs: kind-main (org kind) <-> acme-main (org acme).

    Cross-org, cross-project and cross-namespace at once. The peering service
    has no org awareness whatsoever, so the expectation is that this behaves
    exactly like the same-project case; this pins it. The one visible
    difference is spec.remote.namespace, which is set only when the projects
    differ — asserted by the shared handshake.
    """
    log = Steps("VPC peering: cross-org")
    log.info(f"run_id={run_id} local={project} remote={peer_project}")
    _require_peer_project(session, api_base, region, project, peer_project)

    _peer_cluster_with_instance_group(
        session,
        api_base,
        region,
        log=log,
        run_id=run_id,
        test_name=request.node.name,
        cluster_project=project,
        ig_project=peer_project,
    )
    log.done()


@pytest.mark.crossorg
@pytest.mark.skipif(not auth_configured(), reason="API_BASE required")
def test_vpc_peering_cross_org_owner_teardown(
    session, api_base, region, project, peer_project, run_id, request
):
    """Delete an owner with both peering halves live; only its own side goes.

    The full teardown path, rather than the tidy one the handshake tests use:
    the instance group is deleted while both peerings are still declared, which
    is what a tenant deleting a cluster actually does.

    What it pins (k0rdent-apis `TeardownVPCPeerings`): an owner's teardown
    removes only the peerings whose LOCAL vpc it owns — row tombstoned, UFO CR
    deleted by the owner's own terminate workflow — and leaves the counterpart's
    row and CR deliberately in place, dangling at a VPC that no longer exists.
    Nothing else tests this anywhere: upstream's equivalent runs under
    MOCK_MODE and explicitly disclaims proving that any CR was removed.

    Cross-org only, and not for tidiness — see the module docstring.
    """
    log = Steps("VPC peering: cross-org owner teardown")
    log.info(f"run_id={run_id} local={project} remote={peer_project}")
    _require_peer_project(session, api_base, region, project, peer_project)

    _peer_cluster_with_instance_group(
        session,
        api_base,
        region,
        log=log,
        run_id=run_id,
        test_name=request.node.name,
        cluster_project=project,
        ig_project=peer_project,
        owner_teardown=True,
    )
    log.done()
