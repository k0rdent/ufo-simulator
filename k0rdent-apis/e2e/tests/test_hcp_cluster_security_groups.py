"""HCP cluster + VPC/cluster security-group binding and NICo/UFO materialization."""

from __future__ import annotations

import os
from typing import Any

import pytest

from conftest import (
    auth_configured,
    ensure_global_prereqs,
    load_scenario_template,
)
from helpers import api, k8s, wait
from helpers.steps import Steps


pytestmark = [pytest.mark.smoke, pytest.mark.hcp]

_SCENARIO = "hcp_cluster_security_groups"
_VPC_CUSTOM_SG_ID = "demo-sg"
_CLUSTER_SG_ID = "cluster-sg"


def _await_sg_active(session, sg_collection: str, sg_id: str) -> dict[str, Any]:
    return wait.await_api_state(
        lambda: api.get_json(session, f"{sg_collection}/{sg_id}"),
        "active",
        timeout=300,
        interval=5,
    )


def _ensure_security_group(
    session, sg_collection: str, template_name: str, sg_id: str
) -> dict[str, Any]:
    api.ensure_exists(
        session,
        sg_collection,
        load_scenario_template(_SCENARIO, template_name),
    )
    return _await_sg_active(session, sg_collection, sg_id)


def _fresh_cluster(session, clusters_url: str, cluster: dict[str, Any]) -> None:
    cluster_url = f"{clusters_url}/{cluster['id']}"
    existing = api.get(session, cluster_url)
    if existing.status_code == 200:
        api.delete(session, cluster_url)
        wait.await_api_absent(
            lambda: None if api.get(session, cluster_url).status_code == 404 else True,
            timeout=1800,
            interval=15,
            desc=f"cluster {cluster['id']} gone before recreate",
        )
    elif existing.status_code != 404:
        existing.raise_for_status()

    created = session.post(clusters_url, json=cluster, timeout=60)
    assert created.status_code in (200, 201), created.text


def _cluster_vpcs(
    session,
    api_base: str,
    region: str,
    project: str,
    cluster_uid: str,
) -> list[dict[str, Any]]:
    vpcs_url = api.region_url(api_base, region, "networking/vpcs", project=project)
    items = api.list_items(session, vpcs_url)
    return [
        v
        for v in items
        if v.get("ownerKind") == "cluster"
        and v.get("ownerId") == cluster_uid
        and (v.get("backend") or "").lower() == "nico"
    ]


def _sg_api_rules(sg: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    rules = sg.get("rules") or {}
    return {
        "ingress": list(rules.get("ingress") or []),
        "egress": list(rules.get("egress") or []),
    }


def _rule_fingerprint(rule: dict[str, Any], *, direction: str | None = None) -> tuple:
    """Compare API / UFO / NICo rules ignoring case on protocol/action."""
    return (
        (direction or "").lower(),
        (rule.get("name") or "").lower(),
        (rule.get("protocol") or "").lower(),
        (rule.get("action") or "").lower(),
        rule.get("sourcePrefix") or rule.get("source_prefix") or "",
        rule.get("destinationPrefix") or rule.get("destination_prefix") or "",
        rule.get("sourcePortRange") or rule.get("source_port_range") or "",
        rule.get("destinationPortRange") or rule.get("destination_port_range") or "",
    )


def _api_rule_fingerprints(sg: dict[str, Any]) -> set[tuple]:
    out: set[tuple] = set()
    rules = _sg_api_rules(sg)
    for direction, items in rules.items():
        for rule in items:
            out.add(_rule_fingerprint(rule, direction=direction))
    return out


def _ufo_cr_rule_fingerprints(cr: dict[str, Any]) -> set[tuple]:
    spec = cr.get("spec") or {}
    rules = spec.get("rules") or {}
    out: set[tuple] = set()
    for direction in ("ingress", "egress"):
        for rule in rules.get(direction) or []:
            out.add(_rule_fingerprint(rule, direction=direction))
    return out


def _nsg_rule_fingerprints(nsg: dict[str, Any]) -> set[tuple]:
    """Flatten NICo NetworkSecurityGroup.spec.rules into comparable fingerprints."""
    out: set[tuple] = set()
    for rule in (nsg.get("spec") or {}).get("rules") or []:
        direction = (rule.get("direction") or "").lower()
        name = rule.get("name") or ""
        if name.startswith("ufo-default-"):
            continue
        out.add(_rule_fingerprint(rule, direction=direction))
    return out


def _assert_rules_present(haystack: set[tuple], needle: set[tuple], *, label: str) -> None:
    missing = needle - haystack
    assert not missing, f"{label}: missing rules {missing!r}; have {haystack!r}"


def _assert_ufo_security_group_cr(
    kube, ns: str, sg: dict[str, Any], *, api_id: str
) -> None:
    cr_name = k8s.ufo_security_group_name(sg["uid"])

    def _ufo_sg():
        return k8s.get_custom(
            kube,
            group="ufo.mirantis.com",
            version="v1alpha1",
            plural="securitygroups",
            namespace=ns,
            name=cr_name,
        )

    cr = wait.await_predicate(
        _ufo_sg,
        timeout=300,
        interval=5,
        desc=f"UFO SecurityGroup {cr_name} for API id {api_id}",
    )
    _assert_rules_present(
        _ufo_cr_rule_fingerprints(cr),
        _api_rule_fingerprints(sg),
        label=f"UFO CR {cr_name} rules vs API {api_id}",
    )


def _nsg_named_ingress_order(nsg: dict[str, Any]) -> list[str]:
    """Named INGRESS rules in NICo evaluation order (priority ascending / list order)."""
    rules = list((nsg.get("spec") or {}).get("rules") or [])

    def _priority(rule: dict[str, Any]) -> int:
        p = rule.get("priority")
        return int(p) if p is not None else 10**9

    ingress = [
        r
        for r in rules
        if (r.get("direction") or "").upper() == "INGRESS"
        and (r.get("name") or "")
        and not (r.get("name") or "").startswith("ufo-default-")
    ]
    ingress.sort(key=_priority)
    return [r["name"] for r in ingress]


def _first_index(names: list[str], candidates: set[str]) -> int | None:
    for i, name in enumerate(names):
        if name in candidates:
            return i
    return None


@pytest.mark.skipif(
    not auth_configured(),
    reason="API_BASE required",
)
def test_hcp_cluster_vpc_security_groups(session, api_base, region, project):
    """Create cluster, VPC+cluster SG attach, UFO CRs, NICo merge + precedence."""
    log = Steps("HCP cluster security-group scenario")

    log.step("ensure global prereqs (address-pools + cluster-type; never deleted)")
    ensure_global_prereqs(session, api_base, region)
    log.ok()

    sg_collection = api.region_url(
        api_base, region, "networking/security-groups", project=project
    )
    log.step(f"ensure security groups {_VPC_CUSTOM_SG_ID!r} and {_CLUSTER_SG_ID!r}")
    _ensure_security_group(
        session, sg_collection, "security-group-demo-sg.yaml", _VPC_CUSTOM_SG_ID
    )
    cluster_sg = _ensure_security_group(
        session, sg_collection, "security-group-cluster-sg.yaml", _CLUSTER_SG_ID
    )
    log.ok("both SGs active")

    cluster = load_scenario_template(_SCENARIO, "cluster.yaml")
    cluster_id = cluster["id"]
    clusters_url = api.region_url(api_base, region, "compute/clusters", project=project)
    cluster_url = f"{clusters_url}/{cluster_id}"

    log.step(f"create cluster {cluster_id} (delete leftover if any)")
    _fresh_cluster(session, clusters_url, cluster)
    log.ok("create accepted")

    def _get_cluster():
        return api.get_json(session, cluster_url)

    log.step("wait for cluster API state=active")
    cluster_obj = wait.await_api_state(
        _get_cluster, "active", timeout=1800, interval=15, steps=log, log_every=2
    )
    cluster_uid = cluster_obj["uid"]
    log.info(f"cluster uid={cluster_uid}")

    log.step("find NICo VPC owned by cluster")
    vpcs = _cluster_vpcs(session, api_base, region, project, cluster_uid)
    assert vpcs, f"no nico VPC owned by cluster uid={cluster_uid}"
    vpc = vpcs[0]
    vpc_id = vpc["id"]
    vpc_url = api.region_url(
        api_base, region, f"networking/vpcs/{vpc_id}", project=project
    )
    log.info(f"vpc id={vpc_id}")
    vpc = wait.await_api_state(
        lambda: api.get_json(session, vpc_url),
        "active",
        timeout=900,
        interval=10,
        steps=log,
        log_every=2,
    )

    log.step("assert VPC has only the platform default security group")
    bound = list(vpc.get("securityGroups") or [])
    assert len(bound) == 1, f"expected only default SG on new VPC, got {bound!r}"
    default_sg_id = bound[0]
    assert default_sg_id.endswith("-default"), (
        f"default SG id shape: want *-default, got {default_sg_id!r}"
    )
    default_sg = api.get_json(session, f"{sg_collection}/{default_sg_id}")
    assert default_sg.get("ownerKind") == "vpc", (
        f"default SG ownerKind={default_sg.get('ownerKind')!r}, want vpc"
    )
    assert default_sg.get("state") == "active"
    log.ok(f"default SG {default_sg_id!r} (ownerKind=vpc)")

    log.step(
        f"attach VPC SGs [{_VPC_CUSTOM_SG_ID}, {default_sg_id}] (custom + default)"
    )
    attached = api.set_vpc_security_groups(
        session, vpc_url, [_VPC_CUSTOM_SG_ID, default_sg_id]
    )
    assert list(attached.get("securityGroups") or []) == [
        _VPC_CUSTOM_SG_ID,
        default_sg_id,
    ]
    vpc = wait.await_api_state(
        lambda: api.get_json(session, vpc_url),
        "active",
        timeout=900,
        interval=10,
        steps=log,
        log_every=2,
    )
    assert list(vpc.get("securityGroups") or []) == [_VPC_CUSTOM_SG_ID, default_sg_id]
    log.info("wait for cluster to settle after VPC re-render")
    wait.await_api_state(
        _get_cluster, "active", timeout=900, interval=10, steps=log, log_every=2
    )
    log.ok("VPC binding settled")

    kube = k8s.api_client(os.environ.get("KUBECONFIG"))
    ns = k8s.project_namespace(project)

    log.step(f"assert UFO SecurityGroup CRs in {ns}")
    sgs_by_id = {
        _VPC_CUSTOM_SG_ID: _await_sg_active(session, sg_collection, _VPC_CUSTOM_SG_ID),
        default_sg_id: _await_sg_active(session, sg_collection, default_sg_id),
        _CLUSTER_SG_ID: cluster_sg,
    }
    for sg_id in (_VPC_CUSTOM_SG_ID, default_sg_id):
        log.info(f"check UFO CR for {sg_id} → sg-{sgs_by_id[sg_id]['uid']}")
        _assert_ufo_security_group_cr(kube, ns, sgs_by_id[sg_id], api_id=sg_id)
    log.ok("VPC-attached UFO SGs present with matching rules")

    log.step("wait for NICo NetworkSecurityGroup + config attachment")

    def _nico_nsg_ready():
        items = k8s.list_custom(
            kube,
            group="nico.mirantis.com",
            version="v1alpha1",
            plural="networksecuritygroups",
            namespace=ns,
        ).get("items", [])
        if not items:
            return None
        custom_fps = _api_rule_fingerprints(sgs_by_id[_VPC_CUSTOM_SG_ID])
        for item in items:
            status_id = (item.get("status") or {}).get("id") or ""
            if not status_id:
                continue
            have = _nsg_rule_fingerprints(item)
            if custom_fps and custom_fps <= have:
                return item
        for item in items:
            if (item.get("status") or {}).get("id"):
                return item
        return None

    nsg = wait.await_predicate(
        _nico_nsg_ready,
        timeout=900,
        interval=10,
        desc="NICo NetworkSecurityGroup with backend id",
        steps=log,
        log_every=2,
    )
    nsg_id = (nsg.get("status") or {}).get("id")
    assert nsg_id, f"NetworkSecurityGroup missing status.id: {nsg!r}"
    nsg_name = nsg["metadata"]["name"]
    log.info(f"NSG name={nsg_name} status.id={nsg_id}")

    def _nico_cfg_attached():
        for group, plural in (
            ("ufo.mirantis.com", "niconetworkconfigs"),
            ("nico.mirantis.com", "niconetworkconfigs"),
            ("nico.mirantis.com", "instances"),
        ):
            items = k8s.list_custom(
                kube, group=group, version="v1alpha1", plural=plural, namespace=ns
            ).get("items", [])
            for item in items:
                spec = item.get("spec") or {}
                attached_id = spec.get("networkSecurityGroupId")
                if not attached_id:
                    ref = spec.get("networkSecurityGroupIdRef") or spec.get(
                        "networkSecurityGroup"
                    )
                    if isinstance(ref, dict):
                        attached_id = ref.get("id") or ref.get("byName")
                if attached_id and str(attached_id) == str(nsg_id):
                    return item
        return None

    wait.await_predicate(
        _nico_cfg_attached,
        timeout=900,
        interval=10,
        desc="NICo instance/config with networkSecurityGroupId attached",
        steps=log,
        log_every=2,
    )
    log.ok("NICo has security group attached")

    log.step("assert NICo NSG rules include VPC custom + default")

    def _nsg_has_vpc_merged_rules():
        fresh = k8s.get_custom(
            kube,
            group="nico.mirantis.com",
            version="v1alpha1",
            plural="networksecuritygroups",
            namespace=ns,
            name=nsg_name,
        )
        if not fresh:
            return None
        have = _nsg_rule_fingerprints(fresh)
        want = _api_rule_fingerprints(sgs_by_id[_VPC_CUSTOM_SG_ID]) | _api_rule_fingerprints(
            sgs_by_id[default_sg_id]
        )
        return fresh if want <= have else None

    wait.await_predicate(
        _nsg_has_vpc_merged_rules,
        timeout=900,
        interval=10,
        desc="NICo NSG rules include VPC custom + default SG rules",
        steps=log,
        log_every=2,
    )
    log.ok()

    log.step(f"PATCH cluster securityGroups=[{_CLUSTER_SG_ID}]")
    patched = api.set_cluster_security_groups(session, cluster_url, [_CLUSTER_SG_ID])
    assert list(patched.get("securityGroups") or []) == [_CLUSTER_SG_ID]
    wait.await_api_state(
        _get_cluster, "active", timeout=900, interval=10, steps=log, log_every=2
    )
    cluster_obj = _get_cluster()
    assert list(cluster_obj.get("securityGroups") or []) == [_CLUSTER_SG_ID]
    log.ok("cluster binding settled")

    log.step(f"assert UFO CR for cluster SG {_CLUSTER_SG_ID}")
    sgs_by_id[_CLUSTER_SG_ID] = _await_sg_active(session, sg_collection, _CLUSTER_SG_ID)
    _assert_ufo_security_group_cr(
        kube, ns, sgs_by_id[_CLUSTER_SG_ID], api_id=_CLUSTER_SG_ID
    )
    log.ok()

    cluster_rule_names = {
        r.get("name")
        for r in _sg_api_rules(sgs_by_id[_CLUSTER_SG_ID])["ingress"]
        if r.get("name")
    }
    vpc_rule_names = {
        r.get("name")
        for sg_id in (_VPC_CUSTOM_SG_ID, default_sg_id)
        for r in _sg_api_rules(sgs_by_id[sg_id])["ingress"]
        if r.get("name")
    }
    assert cluster_rule_names, "cluster SG must have named ingress rules for precedence"
    assert "cluster-allow-ssh" in cluster_rule_names

    log.step("assert NICo NSG has cluster+VPC rules; cluster ingress precedes VPC")

    def _nsg_has_cluster_and_precedence():
        fresh = k8s.get_custom(
            kube,
            group="nico.mirantis.com",
            version="v1alpha1",
            plural="networksecuritygroups",
            namespace=ns,
            name=nsg_name,
        )
        if not fresh:
            return None
        have = _nsg_rule_fingerprints(fresh)
        want = (
            _api_rule_fingerprints(sgs_by_id[_CLUSTER_SG_ID])
            | _api_rule_fingerprints(sgs_by_id[_VPC_CUSTOM_SG_ID])
            | _api_rule_fingerprints(sgs_by_id[default_sg_id])
        )
        if not want <= have:
            return None

        order = _nsg_named_ingress_order(fresh)
        cluster_idx = _first_index(order, cluster_rule_names)
        vpc_idx = _first_index(order, vpc_rule_names)
        if cluster_idx is None or vpc_idx is None:
            return None
        if cluster_idx >= vpc_idx:
            return None
        return fresh

    nsg_final = wait.await_predicate(
        _nsg_has_cluster_and_precedence,
        timeout=900,
        interval=10,
        desc="NICo NSG has cluster+VPC rules with cluster precedence",
        steps=log,
        log_every=2,
    )
    order = _nsg_named_ingress_order(nsg_final)
    log.info(f"named ingress order: {order}")
    log.ok("cluster rules have higher precedence than VPC")

    cluster_fps = _api_rule_fingerprints(sgs_by_id[_CLUSTER_SG_ID])
    vpc_custom_fps = _api_rule_fingerprints(sgs_by_id[_VPC_CUSTOM_SG_ID])
    default_fps = _api_rule_fingerprints(sgs_by_id[default_sg_id])

    def _nsg_fresh():
        return k8s.get_custom(
            kube,
            group="nico.mirantis.com",
            version="v1alpha1",
            plural="networksecuritygroups",
            namespace=ns,
            name=nsg_name,
        )

    log.step("detach cluster SG and assert its rules leave the NICo NSG")
    try:
        wait.await_api_state(
            _get_cluster, "active", timeout=300, interval=10, steps=log, log_every=2
        )
        api.set_cluster_security_groups(session, cluster_url, [])
        wait.await_api_state(
            _get_cluster, "active", timeout=900, interval=10, steps=log, log_every=2
        )
        assert list(_get_cluster().get("securityGroups") or []) == []

        def _nsg_without_cluster_rules():
            fresh = _nsg_fresh()
            if not fresh:
                return None
            have = _nsg_rule_fingerprints(fresh)
            # Cluster rules gone; VPC custom + default still present.
            if have & cluster_fps:
                return None
            want = vpc_custom_fps | default_fps
            return fresh if want <= have else None

        wait.await_predicate(
            _nsg_without_cluster_rules,
            timeout=900,
            interval=10,
            desc="NICo NSG dropped cluster SG rules (VPC rules remain)",
            steps=log,
            log_every=2,
        )
        log.ok("cluster-sg rules gone from NICo NSG")

        log.step("detach VPC custom SG ([] → default only) and assert custom rules leave NICo")
        wait.await_api_state(
            lambda: api.get_json(session, vpc_url),
            "active",
            timeout=900,
            interval=10,
            steps=log,
            log_every=2,
        )
        api.set_vpc_security_groups(session, vpc_url, [])
        wait.await_api_state(
            lambda: api.get_json(session, vpc_url),
            "active",
            timeout=900,
            interval=10,
            steps=log,
            log_every=2,
        )
        # Cluster re-renders after VPC binding change.
        wait.await_api_state(
            _get_cluster, "active", timeout=900, interval=10, steps=log, log_every=2
        )
        vpc_after = api.get_json(session, vpc_url)
        remaining = list(vpc_after.get("securityGroups") or [])
        assert remaining == [default_sg_id] or (
            len(remaining) == 1 and remaining[0].endswith("-default")
        ), f"expected only default after [], got {remaining!r}"

        def _nsg_default_only_from_tenant_custom():
            fresh = _nsg_fresh()
            if not fresh:
                return None
            have = _nsg_rule_fingerprints(fresh)
            # Custom VPC + cluster rules must be gone; default VPC rules remain.
            if have & cluster_fps:
                return None
            if have & vpc_custom_fps:
                return None
            return fresh if default_fps <= have else None

        wait.await_predicate(
            _nsg_default_only_from_tenant_custom,
            timeout=900,
            interval=10,
            desc="NICo NSG dropped custom VPC SG rules (default remains)",
            steps=log,
            log_every=2,
        )
        log.ok("demo-sg rules gone from NICo NSG; default rules remain")
    finally:
        log.step(f"DELETE cluster {cluster_id}")
        deleted = api.delete(session, cluster_url)
        assert deleted.status_code in (202, 204), deleted.text
        wait.await_api_absent(
            lambda: None if api.get(session, cluster_url).status_code == 404 else True,
            timeout=1800,
            interval=15,
            desc=f"cluster {cluster_id} deleted",
            steps=log,
            log_every=2,
        )
    log.done()
