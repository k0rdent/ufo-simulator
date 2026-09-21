"""Shared helpers for the VPC peering e2e scenarios.

MOCK BOUNDARY. These helpers require MOCK_MODE **off**
(``lab-inject.sh mock off``). Under mock, VPCPeeringCreate returns before
``vpcPeeringPlan`` and before ``ApplyUFOVpcPeering``, so no UFO VpcPeering CR is
ever applied and every CR assertion fails.

Two things about peering shape the helpers:

  * A one-sided peering programs NO fabric, and that is not an error. Every
    backend looks for a mirrored CR with the endpoints swapped before doing
    anything, so ``state=active`` on one side means "this side's CR was applied",
    not "traffic flows". Callers therefore assert the backend object is ABSENT
    with one side declared and PRESENT once the mirror exists.
  * Endpoints cannot be created directly — there is no tenant create for a VPC;
    they are materialized from a cluster's or instance group's bound
    ClusterType.networkSchema. So the endpoints here come from provisioning a
    cluster and an instance group, which also makes these the only tests
    anywhere that peer across owner kinds (``ownerKind`` is never consulted in
    the peering create path).

A peering can only be torn down once, so the two teardown paths need two pairs.
Every run therefore does both rounds against ONE pair of owners — the owners are
what cost a provision, the peerings are cheap:

  1. Declare both halves, assert, then delete the PEERINGS. The tenant path
     where a peering is withdrawn directly, and the only place the fabric
     object's removal is observable.
  2. Declare a SECOND pair, then delete the OWNERS with both halves still
     declared. What a tenant deleting a cluster actually does, and the only
     place an owner's terminate workflow is exercised.

Both tests run both rounds, with the same arguments; the only thing that varies
between them is which project the instance group goes in.

Lab prerequisites beyond the other scenarios: TWO available ``nico-lab`` servers,
because a cluster and an instance group are alive at the same time.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from conftest import ensure_global_prereqs, load_scenario_template, load_template
from helpers import api, k8s, wait
from helpers.names import resource_id, stamp_id
from helpers.steps import Steps

_SCENARIO = "vpc_peering"

# The nico VPC both global cluster types declare. Selected by schemaEntryId
# rather than by filtering on backend: netris-hcp-ew declares a second,
# netris-backed VPC, and an entry id stays unambiguous even if a type grows a
# second nico VPC.
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
        api.raise_for_status(existing)

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


def _delete_owners(session, owners: list[tuple[str | None, str]], *, log: Steps) -> None:
    """DELETE every owner passed, then await them all together.

    Mirrors the creates: issue all the requests first and wait once, so this
    costs max(cluster, ig) rather than the sum. Owner teardown is the slowest
    step in the scenario, so serializing two of them doubles the worst case for
    nothing. Entries whose url is None (never created) are skipped.

    Batch only what is not being asserted on. Round 2 calls this once per owner
    on purpose: it asserts on the state between the two deletes, which a batch
    would skip straight past.
    """
    live = []
    for url, what in owners:
        if not url:
            continue
        # Skip rather than fire a DELETE that would 404: on the happy path round
        # 2 already removed both owners, and a no-op step here is noise — and one
        # more confirmation prompt under E2E_DEMO_MODE.
        if api.get(session, url).status_code == 404:
            log.info(f"{what} already gone")
            continue
        log.step(f"DELETE {what}")
        deleted = api.delete(session, url)
        assert deleted.status_code in (202, 204, 404), deleted.text
        live.append((url, what))
    if not live:
        return
    wait.await_api_absents(
        {
            what: (lambda u=url: None if api.get(session, u).status_code == 404 else True)
            for url, what in live
        },
        timeout=1800,
        interval=15,
        steps=log,
        log_every=2,
    )


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
        api.raise_for_status(resp)
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


def _get_peering_cr(kube, cr: dict[str, Any]) -> dict[str, Any] | None:
    """Re-read a UFO VpcPeering CR we already located, by name.

    By NAME, deliberately, unlike the initial lookup: once the object has been
    found, re-deriving the spec filter is a way to be wrong. A filter that no
    longer matches reports "absent" — so an absence assertion built on one can
    pass for the wrong reason and prove nothing. Same-project peerings leave
    spec.remote.namespace unset, which is exactly the mismatch that hides.
    """
    meta = cr["metadata"]
    return k8s.get_custom(
        kube,
        "ufo.mirantis.com",
        "v1alpha1",
        "vpcpeerings",
        meta["namespace"],
        meta["name"],
    )


def _await_peering_cr_gone(kube, cr: dict[str, Any], *, log: Steps) -> None:
    """Wait until a UFO VpcPeering CR is really gone.

    The CR carries a finalizer held until its backend cleanup finishes, so
    "deleted" here means really gone, not merely marked for deletion.
    """
    meta = cr["metadata"]

    def _gone():
        return True if _get_peering_cr(kube, cr) is None else None

    wait.await_predicate(
        _gone,
        timeout=600,
        interval=10,
        desc=f"UFO VpcPeering {meta['namespace']}/{meta['name']} gone",
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

    ``fwd`` is the side whose local VPC is ``local_vpc``; ``rev`` is its mirror.
    Mutable and passed in by the caller rather than returned, so a ``finally``
    can clean up whichever sides exist when a run fails partway through.
    """

    # Empty string rather than None for the ids/urls: every caller guards on
    # truthiness anyway, and it keeps them plain `str` for the helpers below.
    def __init__(self) -> None:
        self.fwd_id: str = ""
        self.rev_id: str = ""
        self.fwd_url: str = ""
        self.rev_url: str = ""
        # The CRs as first located. Held so later checks can re-read them by name
        # instead of rebuilding the spec filter that found them.
        self.fwd_cr: dict[str, Any] = {}
        self.rev_cr: dict[str, Any] = {}
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
    suffix: str = "",
    local_project: str,
    local_vpc: dict[str, Any],
    remote_project: str,
    remote_vpc: dict[str, Any],
) -> None:
    """Declare both sides and assert the CRs and the single backend object.

    Deliberately has no teardown of its own: how the pair is torn down is what
    the two rounds of a run differ on, and in one of them it is the thing under
    test.

    ``suffix`` distinguishes one round's peering ids from the next's. A
    tombstoned row keeps its id — only the DIRECTION is freed at tombstone — so
    a second pair between the same two VPCs must not reuse the first's ids.

    Cross-project only in that the two projects may differ: when they do, the
    CRs carry spec.remote.namespace; when they do not, it must be absent.
    """
    cross_project = local_project != remote_project
    local_ns = k8s.project_namespace(local_project)
    remote_ns = k8s.project_namespace(remote_project)

    hs.fwd_id = resource_id(test_name, f"fwd{suffix}", run_id=run_id)
    hs.rev_id = resource_id(test_name, f"rev{suffix}", run_id=run_id)

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
    hs.fwd_cr = fwd_cr
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
    hs.rev_cr = rev_cr
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


def run_handshake(
    session,
    api_base,
    region,
    kube,
    *,
    log: Steps,
    run_id: str,
    test_name: str,
    suffix: str = "",
    local_project: str,
    local_vpc: dict[str, Any],
    remote_project: str,
    remote_vpc: dict[str, Any],
) -> None:
    """Round 1: declare both sides, assert, then delete the PEERINGS.

    The tenant path where a peering is withdrawn directly. Reverse side first,
    so the fabric object's removal is observable: withdrawing either half is
    enough to take it down. :func:`run_owner_teardown` is the other path, where
    the peerings are never deleted by hand.
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
            suffix=suffix,
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


def run_owner_teardown(
    session,
    api_base,
    region,
    kube,
    *,
    log: Steps,
    run_id: str,
    test_name: str,
    suffix: str = "",
    local_project: str,
    local_vpc: dict[str, Any],
    remote_project: str,
    remote_vpc: dict[str, Any],
    local_owner_url: str,
    local_owner_what: str,
    remote_owner_url: str,
    remote_owner_what: str,
) -> None:
    """Round 2: delete BOTH owners in turn, peerings never deleted by hand.

    Two halves, covering the two different teardown implementations:

      1. Delete the remote owner (the instance group, via
         ``providers/ufo/teardown_network.go``). Only its own side may go — row
         tombstoned and UFO CR deleted by its terminate workflow — while the
         counterpart's row and CR are left deliberately in place, dangling at a
         VPC that no longer exists, with no signal to that tenant.
      2. Delete the local owner (the cluster, via
         ``cluster_deployment_terminate_v2.go``). The surviving peering must now
         go **with it**, without the test deleting it — which is what proves the
         cleanup is the owner's, not the test's. The two paths differ: the
         instance-group one waits for its Vpcs, the cluster one does not.

    Nothing else tests either: upstream's equivalent runs under MOCK_MODE and
    disclaims proving that any CR was applied or removed.
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
            suffix=suffix,
            local_project=local_project,
            local_vpc=local_vpc,
            remote_project=remote_project,
            remote_vpc=remote_vpc,
        )

        # One owner at a time, and NOT batched with the cluster delete below:
        # the contract is only observable in the window where this owner is gone
        # and the other is still alive. Deleting both together would leave
        # nothing to attribute.
        log.info("both peering sides still declared — deleting the remote owner")
        _delete_owners(session, [(remote_owner_url, remote_owner_what)], log=log)

        log.step(f"assert the torn-down owner's side ({hs.rev_id}) is gone")
        _await_absent(
            session,
            hs.rev_url,
            desc=f"peering {hs.rev_id} removed with its owner",
            log=log,
            timeout=900,
        )
        _await_peering_cr_gone(kube, hs.rev_cr, log=log)
        hs.rev_url = ""  # already gone; keep the finally from re-deleting it
        log.ok("row and CR both removed by the owner's teardown")

        log.step(f"assert the counterpart ({hs.fwd_id}) SURVIVES, dangling")
        survivor = api.get(session, hs.fwd_url)
        assert survivor.status_code == 200, (
            f"the counterpart must outlive the other owner, got {survivor.status_code}: "
            f"{survivor.text}"
        )
        assert _get_peering_cr(kube, hs.fwd_cr) is not None, (
            "the counterpart's UFO CR must be left in place"
        )
        log.ok(f"row state={survivor.json().get('state')!r}, CR still present")

        log.step("assert the fabric peering went with the torn-down side")
        _await_no_backend_peering(kube, hs.crs, log=log)
        log.ok()

        # Second half: the surviving peering is still live and still declared.
        # Deleting its owner — never the peering itself — is what proves the
        # cleanup belongs to the owner's terminate workflow. This also exercises
        # the cluster path, which unlike the instance-group one does not wait
        # for its Vpcs.
        log.info("the surviving peering is still declared — deleting its owner")
        _delete_owners(session, [(local_owner_url, local_owner_what)], log=log)

        log.step(f"assert the surviving side ({hs.fwd_id}) went with its owner")
        _await_absent(
            session,
            hs.fwd_url,
            desc=f"peering {hs.fwd_id} removed with its owner",
            log=log,
            timeout=900,
        )
        _await_peering_cr_gone(kube, hs.fwd_cr, log=log)
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


def peer_cluster_with_instance_group(
    session,
    api_base,
    region,
    *,
    log: Steps,
    run_id: str,
    test_name: str,
    cluster_project: str,
    ig_project: str,
) -> None:
    """Peer a cluster's nico VPC with an instance group's, both teardown paths.

    Both tests are this same scenario, called with the same arguments; the only
    thing that varies is which project the instance group goes in. Same project
    as the cluster gives the plain handshake (spec.remote.namespace absent); a
    project in another org gives the cross-org case (spec.remote.namespace set).

    One provision, two rounds — :func:`run_handshake` deletes the peerings,
    then :func:`run_owner_teardown` declares a second pair and deletes the
    owners. Splitting them across two tests would buy nothing and cost a second
    cluster + instance group.

    Resources are created inline and cleaned up in ``finally``, matching the rest
    of the suite; nothing is shared across tests.
    """
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

        # Round 1: withdraw the peerings themselves.
        run_handshake(
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
        )

        # Round 2: a fresh pair — round 1's is tombstoned — torn down by
        # deleting the owners instead.
        run_owner_teardown(
            session,
            api_base,
            region,
            kube,
            log=log,
            run_id=run_id,
            test_name=test_name,
            suffix="2",
            local_project=cluster_project,
            local_vpc=cluster_vpc,
            remote_project=ig_project,
            remote_vpc=ig_vpc,
            local_owner_url=cluster_url,
            local_owner_what=f"cluster {cluster_id}",
            remote_owner_url=ig_url,
            remote_owner_what=f"instance group {ig_id}",
        )
    finally:
        # Both at once, the mirror of the creates above. On the happy path round
        # 2 already removed them and every DELETE here answers 404, so this is a
        # safety net for runs that failed earlier — which is exactly when two
        # owners are still alive and serializing the waits would hurt most.
        _delete_owners(
            session,
            [
                (ig_url, f"instance group {ig_id}"),
                (cluster_url, f"cluster {cluster_id}"),
            ],
            log=log,
        )


# --------------------------------------------------------------------------
# preflight
# --------------------------------------------------------------------------


def require_peer_project(session, api_base, region, project: str, peer_project: str) -> None:
    """Skip unless a second, distinct project is reachable for the cross-org test."""
    if peer_project == project:
        pytest.skip("E2E_PEER_PROJECT must name a project other than PROJECT")
    probe = api.get(
        session,
        api.region_url(api_base, region, "compute/instance-groups", project=peer_project),
    )
    if probe.status_code == 404:
        pytest.skip(f"peer project {peer_project!r} not present on this lab")
    api.raise_for_status(probe)
