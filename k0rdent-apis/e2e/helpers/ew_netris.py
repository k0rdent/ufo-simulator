"""Netris-specific half of the east-west fabric assertions.

When an owner owns a VPC on the netris backend, UFO renders per-machine
``NicoNetworkConfig.spec.networkv2`` with concrete addresses/routes on ``eth-ew*``
interfaces, and the netris backend creates ``LinkAttachment`` objects owned by
each machine's ``ServerNICAttachment``. Success for those attachments is
``status.status=Applied`` (see netris-operator linkattachment_translations).

The netplan half is backend-agnostic and lives in ``ew_common``; only the
LinkAttachment check below is netris's own.
"""

from __future__ import annotations

from typing import Any

from helpers import ew_common, k8s
from helpers.ew_common import assert_ew_networkv2_resolved  # noqa: F401 (re-export)
from helpers.steps import Steps

NETRIS_BACKEND = "netris"


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


def _await(
    kube,
    namespace: str,
    *,
    find_configs,
    log: Steps,
    timeout: int,
    interval: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    configs, attachments = ew_common.await_ew_ready(
        find_configs=find_configs,
        backend_check=lambda bundles: _link_attachments_for_bundles(
            kube, namespace, bundles
        ),
        desc="wait for NicoNetworkConfig eth-ew* IPs/routes + LinkAttachment Applied",
        log=log,
        timeout=timeout,
        interval=interval,
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
    expected_machines: int = 1,
    timeout: int = 900,
    interval: int = 10,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Wait until every CAPI Machine for the ClusterDeployment has EW netplan + LAs."""
    return _await(
        kube,
        namespace,
        find_configs=ew_common.cluster_config_finder(
            kube,
            namespace,
            cluster_deployment_name=cluster_deployment_name,
            expected_machines=expected_machines,
        ),
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
    """Wait until IG NetworkBundle machines have EW netplan + Applied LAs."""
    return _await(
        kube,
        namespace,
        find_configs=ew_common.instance_group_config_finder(
            kube, namespace, instance_group_uid=instance_group_uid
        ),
        log=log,
        timeout=timeout,
        interval=interval,
    )
