"""East-west fabric assertions shared by every fabric backend.

Whatever the backend, an east-west cluster produces the same two things: UFO
renders per-machine ``NicoNetworkConfig.spec.networkv2`` with concrete
addresses and routes on ``eth-ew*``, and the backend creates its own per-host
object to prove it programmed the fabric. Only the second differs, so it is the
one thing the wait below takes as a parameter.

``ew_netris`` and ``ew_verity`` supply that parameter; everything else --
resolving configs for a cluster or an instance group, checking the rendered
netplan, driving the poll -- lives here.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Callable
from typing import Any

from helpers import k8s, wait
from helpers.steps import Steps


def _is_resolved_cidr(value: str) -> bool:
    try:
        ipaddress.ip_interface(value)
    except ValueError:
        return False
    return True


def _is_resolved_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def assert_ew_networkv2_resolved(cfg: dict[str, Any]) -> list[str]:
    """Require every ``eth-ew*`` stanza to have concrete addresses and routes.

    A symbolic reference that survived into the rendered config (``ipFromSubnet``,
    ``cidrFromRailsubnet``, ``gatewayFromSubnet``) means the allocator never
    resolved it, which is exactly the failure this is here to catch.

    Returns the EW interface names that were checked.
    """
    name = (cfg.get("metadata") or {}).get("name")
    networkv2 = (cfg.get("spec") or {}).get("networkv2") or {}
    ew = k8s.ew_ethernets(networkv2)
    assert ew, (
        f"NicoNetworkConfig {name!r} has no eth-ew* ethernets in "
        f"spec.networkv2.ethernets={list((networkv2.get('ethernets') or {}).keys())!r}"
    )
    for ifname, eth in ew.items():
        addrs = list(eth.get("addresses") or [])
        assert addrs, f"{name}/{ifname}: expected resolved addresses, got none"
        for addr in addrs:
            assert isinstance(addr, str) and _is_resolved_cidr(addr), (
                f"{name}/{ifname}: address {addr!r} is not a resolved CIDR"
            )
        routes = list(eth.get("routes") or [])
        assert routes, f"{name}/{ifname}: expected resolved routes, got none"
        for route in routes:
            to = route.get("to")
            via = route.get("via")
            assert isinstance(to, str) and to, (
                f"{name}/{ifname}: route missing resolved 'to': {route!r}"
            )
            # netplan may use "default"; otherwise require a CIDR/IP destination.
            if to not in ("default", "0.0.0.0/0", "::/0"):
                assert _is_resolved_cidr(to) or _is_resolved_ip(to), (
                    f"{name}/{ifname}: route.to {to!r} is not resolved"
                )
            assert isinstance(via, str) and _is_resolved_ip(via), (
                f"{name}/{ifname}: route.via {via!r} is not a resolved IP: {route!r}"
            )
    return sorted(ew)


def bundle_names_for_configs(configs: list[dict[str, Any]]) -> set[str]:
    """NetworkBundle names stamped on a set of NicoNetworkConfigs."""
    names: set[str] = set()
    for cfg in configs:
        labels = (cfg.get("metadata") or {}).get("labels") or {}
        bundle = labels.get(k8s.LABEL_NETWORK_BUNDLE)
        if bundle:
            names.add(bundle)
    return names


def cluster_config_finder(
    kube,
    namespace: str,
    *,
    cluster_deployment_name: str,
    expected_machines: int = 1,
) -> Callable[[], list[dict[str, Any]] | None]:
    """Configs for every CAPI Machine of a ClusterDeployment, or None.

    ``expected_machines`` is not redundant with "every machine has a config".
    Machines appear one at a time, so on a multi-node cluster there is a window
    where the first machine exists and is fully rendered while the rest have not
    been created yet -- without the count this returns a complete-looking set of
    one and the caller asserts half a cluster.
    """

    def _find() -> list[dict[str, Any]] | None:
        machines = k8s.list_capi_machines(
            kube, namespace, cluster_name=cluster_deployment_name
        )
        if len(machines) < expected_machines:
            return None
        configs: list[dict[str, Any]] = []
        for machine in machines:
            cfg = k8s.nico_network_config_for_machine(kube, namespace, machine)
            if not cfg:
                return None
            configs.append(cfg)
        return configs

    return _find


def instance_group_config_finder(
    kube,
    namespace: str,
    *,
    instance_group_uid: str,
) -> Callable[[], list[dict[str, Any]] | None]:
    """Configs for an instance group's NetworkBundles, or None.

    Instance-group create names NetworkBundles ``nb-<ig-uid>-<poolSlug>``
    (k0rdent-apis ``NetworkBundleName``) and stamps that name on
    NicoNetworkConfig / ServerNICAttachment via ``ufo.mirantis.com/networkbundle``.
    """
    uid = (instance_group_uid or "").strip().lower()
    assert uid, "instance_group_uid is required"
    bundle_prefix = f"nb-{uid}-"

    def _find() -> list[dict[str, Any]] | None:
        configs = [
            cfg
            for cfg in k8s.list_ufo_nico_network_configs(kube, namespace)
            if (
                ((cfg.get("metadata") or {}).get("labels") or {}).get(
                    k8s.LABEL_NETWORK_BUNDLE
                )
                or ""
            ).startswith(bundle_prefix)
        ]
        return configs or None

    return _find


def await_ew_ready(
    *,
    find_configs: Callable[[], list[dict[str, Any]] | None],
    backend_check: Callable[[set[str]], Any | None],
    desc: str,
    log: Steps,
    timeout: int = 900,
    interval: int = 10,
) -> tuple[list[dict[str, Any]], Any]:
    """Wait for resolved eth-ew* netplan plus the backend's own per-host object.

    ``backend_check`` takes the NetworkBundle names seen on the configs and
    returns the backend objects once they are all ready, or None to keep
    waiting. Everything before it is backend-agnostic.
    """

    def _pred():
        configs = find_configs()
        if not configs:
            return None
        try:
            for cfg in configs:
                assert_ew_networkv2_resolved(cfg)
        except AssertionError:
            # Not a failure: the config is re-rendered on every allocation
            # reconcile, so an address can be briefly absent.
            return None

        backend_objs = backend_check(bundle_names_for_configs(configs))
        if backend_objs is None:
            return None
        return configs, backend_objs

    log.step(desc)
    configs, backend_objs = wait.await_predicate(
        _pred,
        timeout=timeout,
        interval=interval,
        desc=desc,
        steps=log,
        log_every=2,
    )
    # Re-assert after the wait: the predicate swallows AssertionError to poll,
    # so without this a config that regressed between the last poll and here
    # would go unnoticed.
    for cfg in configs:
        ifaces = assert_ew_networkv2_resolved(cfg)
        mid = ((cfg.get("metadata") or {}).get("labels") or {}).get(
            k8s.LABEL_NICO_MACHINE_ID
        )
        log.info(
            f"NicoNetworkConfig {cfg['metadata']['name']} "
            f"machine-id={mid!r} ew={ifaces}"
        )
    return configs, backend_objs
