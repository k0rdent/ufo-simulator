"""Assertions for Netris east-west fabric after cluster / instance-group Ready.

When an owner owns a VPC on the netris backend, UFO renders per-machine
``NicoNetworkConfig.spec.networkv2`` with concrete addresses/routes on ``eth-ew*``
interfaces, and the netris backend creates ``LinkAttachment`` objects owned by
each machine's ``ServerNICAttachment``. Success for those attachments is
``status.status=Applied`` (see netris-operator linkattachment_translations).
"""

from __future__ import annotations

import ipaddress
from collections.abc import Callable
from typing import Any

from helpers import k8s, wait
from helpers.steps import Steps

NETRIS_BACKEND = "netris"


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


def _link_attachments_for_bundles(
    kube, namespace: str, bundle_names: set[str]
) -> list[dict[str, Any]] | None:
    """Non-provisioning LinkAttachments owned by SNAs for the given bundles.

    Returns ``None`` while SNAs or Applied attachments are still missing.
    """
    if not bundle_names:
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
        return None

    related: list[dict[str, Any]] = []
    for la in k8s.list_link_attachments(kube, namespace):
        if k8s.is_provisioning_link_attachment(la):
            continue
        if any(k8s.owns(sna, la) for sna in snas):
            related.append(la)
    if not related:
        return None
    if not all(k8s.link_attachment_applied(la) for la in related):
        return None
    return related


def _await_ew_netris_ready(
    kube,
    namespace: str,
    *,
    find_configs: Callable[[], list[dict[str, Any]] | None],
    log: Steps,
    timeout: int = 900,
    interval: int = 10,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Shared wait: resolved eth-ew* netplan + Applied LinkAttachments."""

    def _pred():
        configs = find_configs()
        if not configs:
            return None
        try:
            for cfg in configs:
                assert_ew_networkv2_resolved(cfg)
        except AssertionError:
            return None

        bundle_names: set[str] = set()
        for cfg in configs:
            labels = (cfg.get("metadata") or {}).get("labels") or {}
            bundle = labels.get(k8s.LABEL_NETWORK_BUNDLE)
            if bundle:
                bundle_names.add(bundle)
        related = _link_attachments_for_bundles(kube, namespace, bundle_names)
        if related is None:
            return None
        return configs, related

    log.step(
        "wait for NicoNetworkConfig eth-ew* IPs/routes + LinkAttachment Applied"
    )
    configs, attachments = wait.await_predicate(
        _pred,
        timeout=timeout,
        interval=interval,
        desc="EW NicoNetworkConfig + LinkAttachments Applied",
        steps=log,
        log_every=2,
    )
    for cfg in configs:
        ifaces = assert_ew_networkv2_resolved(cfg)
        mid = ((cfg.get("metadata") or {}).get("labels") or {}).get(
            k8s.LABEL_NICO_MACHINE_ID
        )
        log.info(
            f"NicoNetworkConfig {cfg['metadata']['name']} "
            f"machine-id={mid!r} ew={ifaces}"
        )
    for la in attachments:
        st = (la.get("status") or {}).get("status")
        log.info(
            f"LinkAttachment {la['metadata']['name']} "
            f"status={st!r} port={(la.get('spec') or {}).get('name')!r}"
        )
    log.ok(
        f"{len(configs)} NicoNetworkConfig(s), "
        f"{len(attachments)} LinkAttachment(s) Applied"
    )
    return configs, attachments


def await_cluster_ew_netris_ready(
    kube,
    namespace: str,
    *,
    cluster_deployment_name: str,
    log: Steps,
    timeout: int = 900,
    interval: int = 10,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Wait until every CAPI Machine for the ClusterDeployment has EW netplan + LAs."""

    def _find_configs() -> list[dict[str, Any]] | None:
        machines = k8s.list_capi_machines(
            kube, namespace, cluster_name=cluster_deployment_name
        )
        if not machines:
            return None
        configs: list[dict[str, Any]] = []
        for machine in machines:
            cfg = k8s.nico_network_config_for_machine(kube, namespace, machine)
            if not cfg:
                return None
            configs.append(cfg)
        return configs

    return _await_ew_netris_ready(
        kube,
        namespace,
        find_configs=_find_configs,
        log=log,
        timeout=timeout,
        interval=interval,
    )


def await_instance_group_ew_netris_ready(
    kube,
    namespace: str,
    *,
    instance_group_uid: str,
    log: Steps,
    timeout: int = 900,
    interval: int = 10,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Wait until IG NetworkBundle machines have EW netplan + Applied LAs.

    Instance-group create names NetworkBundles ``nb-<ig-uid>-<poolSlug>``
    (k0rdent-apis ``NetworkBundleName``) and stamps that name on
    NicoNetworkConfig / ServerNICAttachment via ``ufo.mirantis.com/networkbundle``.
    """
    uid = (instance_group_uid or "").strip().lower()
    assert uid, "instance_group_uid is required"
    bundle_prefix = f"nb-{uid}-"

    def _find_configs() -> list[dict[str, Any]] | None:
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

    return _await_ew_netris_ready(
        kube,
        namespace,
        find_configs=_find_configs,
        log=log,
        timeout=timeout,
        interval=interval,
    )
