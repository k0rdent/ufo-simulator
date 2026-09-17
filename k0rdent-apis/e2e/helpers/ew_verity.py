"""Verity-specific half of the east-west fabric assertions (Spectrum-X).

Where the netris backend creates a ``LinkAttachment`` per port, UFO's verity
backend on a Spectrum-X fabric creates exactly one ``HGXTenantAssignment`` per
host, owned by that host's ``ServerNICAttachment``. verity-operator sets
``ReconcileReady=True`` on it only after it has PATCHed the switchpoint tenant
against the live Verity API, so True genuinely means "programmed on the fabric".

On top of that this module asserts the ``P2P`` objects, which is the part that
actually proves rail-optimised addressing worked: UFO and the fabric controller
each derive a /31 per link from the same subnet base, independently. If they
disagree the link never comes up, and the only place that disagreement is
visible before traffic is the pair of addresses recorded here.

Nothing asserts absolute addresses. Which machines the scheduler picks varies
run to run (NicoMachineTemplate is annotated ``random-instance-type``), so the
invariant is the shape: host even, switch = host + 1, both in one /31, host
matching the rendered netplan.
"""

from __future__ import annotations

import ipaddress
from typing import Any

from helpers import ew_common, k8s
from helpers.steps import Steps


class _Stall:
    """Log why a wait is still waiting, but only when the reason changes.

    await_predicate reports ``last=None`` on timeout, which after twenty
    minutes says nothing. The verity path has several distinct stalls that look
    identical from outside -- no attachment yet, attachment without host
    coordinates, assignment with no conditions, assignment reporting an error --
    so name whichever one we are in.
    """

    def __init__(self, log: Steps) -> None:
        self._log = log
        self._last: str | None = None

    def __call__(self, reason: str) -> None:
        if reason != self._last:
            self._log.info(f"waiting: {reason}")
            self._last = reason


def _host_coords(sna: dict[str, Any]) -> tuple[str, str]:
    labels = (sna.get("metadata") or {}).get("labels") or {}
    return (
        (labels.get(k8s.LABEL_SU_ID) or "").strip(),
        (labels.get(k8s.LABEL_HOST_ID) or "").strip(),
    )


def hgx_assignments_for_bundles(
    kube,
    namespace: str,
    bundle_names: set[str],
    *,
    stall: _Stall | None = None,
) -> list[dict[str, Any]] | None:
    """Ready HGXTenantAssignments owned by the SNAs for the given bundles.

    Returns ``None`` while anything is still missing or not ready. There are no
    labels on an HGXTenantAssignment, so the owner reference is the only join.
    """

    def _stall(reason: str) -> None:
        if stall is not None:
            stall(reason)

    if not bundle_names:
        _stall("no NetworkBundle names on the rendered configs yet")
        return None

    snas: list[dict[str, Any]] = []
    for bundle in sorted(bundle_names):
        snas.extend(
            k8s.list_servernicattachments(
                kube,
                namespace,
                label_selector=f"{k8s.LABEL_NETWORK_BUNDLE}={bundle}",
            )
        )
    if not snas:
        _stall(f"no ServerNICAttachment for bundles {sorted(bundle_names)}")
        return None

    # UFO skips creating the assignment altogether, without erroring, when it
    # cannot read su-id/host-id -- those come from Server.status.location, which
    # needs Link CRs carrying both. Waiting here is correct; silence is not.
    unlocated = [
        (sna.get("metadata") or {}).get("name")
        for sna in snas
        if not all(_host_coords(sna))
    ]
    if unlocated:
        _stall(f"ServerNICAttachment(s) without su-id/host-id: {sorted(unlocated)}")
        return None

    assignments = k8s.list_hgx_tenant_assignments(kube, namespace)
    related = [hgx for hgx in assignments if any(k8s.owns(sna, hgx) for sna in snas)]
    if len(related) < len(snas):
        _stall(
            f"{len(related)}/{len(snas)} HGXTenantAssignment(s) created "
            f"for {len(snas)} attachment(s)"
        )
        return None

    not_ready = [hgx for hgx in related if not k8s.reconcile_ready(hgx)]
    if not_ready:
        _stall(
            "HGXTenantAssignment not ready: "
            + ", ".join(sorted(_hgx_state(hgx) for hgx in not_ready))
        )
        return None
    return related


def _hgx_state(hgx: dict[str, Any]) -> str:
    """One-line reason an assignment is not ready, for the stall message."""
    name = (hgx.get("metadata") or {}).get("name")
    cond = k8s.condition(hgx, "ReconcileReady")
    if not cond:
        # The reconciler soft-requeues while the referenced verity Tenant is
        # missing and leaves conditions untouched, so this is a normal state to
        # pass through -- not an error.
        return f"{name}=no-conditions-yet (waiting for its verity Tenant?)"
    return (
        f"{name}={cond.get('status')}"
        f" reason={cond.get('reason')!r} msg={cond.get('message')!r}"
    )


def assert_no_stuck_hgx(kube, namespace: str, log: Steps) -> None:
    """Fail fast on assignments left behind by an earlier run.

    HGXTenantAssignment is named per *physical host*, not per run, and carries a
    verity-operator finalizer. If a previous run leaked one, or its delete stalled
    because the Verity API was unreachable, UFO's next attempt to adopt the same
    name fails with AlreadyOwnedError forever -- which surfaces twenty minutes
    later as an unhelpful "HGX not ready". Say so now instead.

    This only reports. The suite never deletes or patches operator-owned CRs.
    """
    log.step("check for HGXTenantAssignments left over from an earlier run")
    assignments = k8s.list_hgx_tenant_assignments(kube, namespace)
    sna_uids = {
        (sna.get("metadata") or {}).get("uid")
        for sna in k8s.list_servernicattachments(kube, namespace)
    }

    terminating, orphaned = [], []
    for hgx in assignments:
        meta = hgx.get("metadata") or {}
        if meta.get("deletionTimestamp"):
            terminating.append(meta.get("name"))
            continue
        owners = [
            ref for ref in (meta.get("ownerReferences") or []) if ref.get("controller")
        ]
        if owners and owners[0].get("uid") not in sna_uids:
            orphaned.append(meta.get("name"))

    assert not terminating and not orphaned, (
        "stale HGXTenantAssignment(s) in "
        f"{namespace}: terminating={sorted(terminating)!r} "
        f"orphaned={sorted(orphaned)!r}. These block adoption by a new "
        "ServerNICAttachment (AlreadyOwnedError) and must be cleared before "
        "this test can pass. Check whether verity-operator can reach the "
        "Verity API, then remove them by hand."
    )
    log.ok(f"{len(assignments)} assignment(s) present, none stale")


def p2ps_for_configs(
    kube, namespace: str, configs: list[dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    """Map NicoNetworkConfig name -> the P2P objects belonging to that machine.

    The join is by owner reference, via the allocation: P2P is owned by a
    NicoNetworkConfigAllocation whose immutable spec.machineId and
    spec.networkBundleName match the config's labels. The label-based route
    (``ufo.mirantis.com/p2p-owner-bmh-name``) only exists on the Metal3 path,
    not the NICo one this cluster uses.
    """
    allocs = k8s.list_nico_network_config_allocations(kube, namespace)
    p2ps = k8s.list_p2ps(kube, namespace)

    by_config: dict[str, list[dict[str, Any]]] = {}
    for cfg in configs:
        meta = cfg.get("metadata") or {}
        labels = meta.get("labels") or {}
        machine_id = labels.get(k8s.LABEL_NICO_MACHINE_ID)
        bundle = labels.get(k8s.LABEL_NETWORK_BUNDLE)
        matched = [
            alloc
            for alloc in allocs
            if (alloc.get("spec") or {}).get("machineId") == machine_id
            and (alloc.get("spec") or {}).get("networkBundleName") == bundle
        ]
        owned: list[dict[str, Any]] = []
        for alloc in matched:
            owned.extend(p2p for p2p in p2ps if k8s.owns(alloc, p2p))
        by_config[meta.get("name")] = owned
    return by_config


def assert_p2p_pairs(configs: list[dict[str, Any]], by_config: dict, log: Steps) -> int:
    """Assert each P2P is a well-formed /31 matching the rendered netplan.

    Returns the number of P2Ps checked.
    """
    checked = 0
    for cfg in configs:
        name = (cfg.get("metadata") or {}).get("name")
        ew = k8s.ew_ethernets((cfg.get("spec") or {}).get("networkv2") or {})
        p2ps = by_config.get(name) or []

        assert len(p2ps) == len(ew), (
            f"{name}: expected one P2P per east-west interface "
            f"({len(ew)}: {sorted(ew)}), found {len(p2ps)}: "
            f"{sorted((p.get('metadata') or {}).get('name') for p in p2ps)!r}. "
            "If this is 0, the owner-reference join found no "
            "NicoNetworkConfigAllocation for this machine."
        )

        # host address -> (ifname, stanza), to match each P2P to its interface.
        by_addr = {
            str(ipaddress.ip_interface(addr).ip): (ifname, eth)
            for ifname, eth in ew.items()
            for addr in (eth.get("addresses") or [])
        }

        for p2p in p2ps:
            spec = p2p.get("spec") or {}
            pname = (p2p.get("metadata") or {}).get("name")
            host_cidr = (spec.get("host") or {}).get("address")
            switch_cidr = (spec.get("switch") or {}).get("address")
            assert host_cidr and switch_cidr, (
                f"{pname}: P2P missing an address: host={host_cidr!r} "
                f"switch={switch_cidr!r}"
            )

            host = ipaddress.ip_interface(host_cidr)
            switch = ipaddress.ip_interface(switch_cidr)

            assert host.network.prefixlen == 31 and switch.network.prefixlen == 31, (
                f"{pname}: expected two /31s, got host={host_cidr} "
                f"switch={switch_cidr}"
            )
            assert host.network == switch.network, (
                f"{pname}: host {host_cidr} and switch {switch_cidr} are not "
                "two halves of one /31"
            )
            assert int(host.ip) % 2 == 0, (
                f"{pname}: host {host.ip} should be the even half of the /31"
            )
            assert int(switch.ip) == int(host.ip) + 1, (
                f"{pname}: switch {switch.ip} should be host {host.ip} + 1"
            )

            match = by_addr.get(str(host.ip))
            assert match, (
                f"{pname}: P2P host address {host.ip} is on no east-west "
                f"interface of {name}; netplan has {sorted(by_addr)!r}"
            )
            ifname, eth = match
            for route in eth.get("routes") or []:
                assert route.get("via") == str(switch.ip), (
                    f"{name}/{ifname}: route via {route.get('via')!r} does not "
                    f"match the P2P switch address {switch.ip} from {pname}"
                )

            log.info(
                f"P2P {pname}: {ifname} host={host_cidr} switch={switch_cidr} "
                f"port={(spec.get('switch') or {}).get('port')!r}@"
                f"{(spec.get('switch') or {}).get('name')!r}"
            )
            checked += 1
    return checked


def await_cluster_ew_verity_ready(
    kube,
    namespace: str,
    *,
    cluster_deployment_name: str,
    log: Steps,
    expected_machines: int = 1,
    timeout: int = 1200,
    interval: int = 15,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Wait for EW netplan + ready HGXTenantAssignments, then assert the P2Ps."""
    stall = _Stall(log)
    configs, assignments = ew_common.await_ew_ready(
        find_configs=ew_common.cluster_config_finder(
            kube,
            namespace,
            cluster_deployment_name=cluster_deployment_name,
            expected_machines=expected_machines,
        ),
        backend_check=lambda bundles: hgx_assignments_for_bundles(
            kube, namespace, bundles, stall=stall
        ),
        desc=(
            "wait for NicoNetworkConfig eth-ew* IPs/routes + "
            "HGXTenantAssignment ReconcileReady"
        ),
        log=log,
        timeout=timeout,
        interval=interval,
    )
    for hgx in assignments:
        spec = hgx.get("spec") or {}
        log.info(
            f"HGXTenantAssignment {hgx['metadata']['name']} "
            f"hgx={spec.get('hgx')!r} tenant={spec.get('tenant')!r} ready"
        )
    log.ok(
        f"{len(configs)} NicoNetworkConfig(s), "
        f"{len(assignments)} HGXTenantAssignment(s) ready"
    )

    log.step("assert P2P /31s match the rendered east-west addresses")
    checked = assert_p2p_pairs(configs, p2ps_for_configs(kube, namespace, configs), log)
    log.ok(f"{checked} P2P(s) consistent with netplan")
    return configs, assignments
