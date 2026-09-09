"""Instance group + VPC/IG security-group binding, NICo/UFO materialization, and
the security-groups-effective read (KNF-469).

Same reasoning as the HCP scenario: ``objectKind=instance_group`` is the merge arm
the upstream MOCK_MODE suite cannot reach, because it cannot create an instance
group to bind groups to.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from conftest import (
    auth_configured,
    ensure_global_prereqs,
    load_scenario_template,
)
from helpers import api, k8s, secgroups, wait
from helpers.names import resource_id, stamp_id
from helpers.steps import Steps


pytestmark = [pytest.mark.smoke, pytest.mark.bmaas]

_SCENARIO = "instance_group_security_groups"


@pytest.mark.skipif(
    not auth_configured(),
    reason="API_BASE required",
)
def test_instance_group_vpc_security_groups(
    session, api_base, region, project, run_id, request
):
    """Create IG, VPC+IG SG attach, UFO CRs, NICo merge + precedence, and the
    effective read at every binding stage."""
    log = Steps("Instance group security-group scenario")
    log.info(f"run_id={run_id}")

    log.step("ensure global prereqs (address-pools + cluster-types; never deleted)")
    ensure_global_prereqs(session, api_base, region)
    log.ok()

    log.step("probe security-groups-effective (KNF-469)")
    effective_ready = secgroups.effective_endpoint_available(
        session, api_base, region, project
    )
    log.ok(
        "endpoint available"
        if effective_ready
        else "endpoint ABSENT — this k0rdent-apis build predates KNF-469; "
        "effective assertions skipped"
    )

    def _effective(kind: str, object_id: str) -> dict[str, Any]:
        return secgroups.get_effective(
            session, api_base, region, project, kind=kind, object_id=object_id
        )

    vpc_custom_sg_id = resource_id(request.node.name, "demo-sg", run_id=run_id)
    ig_sg_id = resource_id(request.node.name, "ig-sg", run_id=run_id)
    ig_id = resource_id(request.node.name, "ig", run_id=run_id)

    sg_collection = api.region_url(
        api_base, region, "networking/security-groups", project=project
    )
    log.step(f"ensure security groups {vpc_custom_sg_id!r} and {ig_sg_id!r}")
    secgroups.ensure_security_group(
        session, sg_collection, _SCENARIO, "security-group-demo-sg.yaml", vpc_custom_sg_id
    )
    ig_sg = secgroups.ensure_security_group(
        session, sg_collection, _SCENARIO, "security-group-ig-sg.yaml", ig_sg_id
    )
    log.ok("both SGs active")

    ig = stamp_id(load_scenario_template(_SCENARIO, "instance-group.yaml"), ig_id)
    groups_url = api.region_url(
        api_base, region, "compute/instance-groups", project=project
    )
    ig_url = f"{groups_url}/{ig_id}"

    log.step(f"create instance group {ig_id} (delete leftover if any)")
    secgroups.fresh_resource(session, groups_url, ig, what="instance group")
    log.ok("create accepted")

    def _get_ig():
        return api.get_json(session, ig_url)

    log.step("wait for instance group API state=active")
    ig_obj = wait.await_api_state(
        _get_ig,
        "active",
        what=f"instance group {ig_id}",
        timeout=1800,
        interval=15,
        steps=log,
        log_every=2,
    )
    ig_uid = ig_obj["uid"]
    log.info(f"instance group uid={ig_uid}")

    default_sg_id: str | None = None
    default_sg_uid: str | None = None

    log.step("find NICo VPC owned by instance group")
    vpcs = secgroups.owner_vpcs(
        session,
        api_base,
        region,
        project,
        owner_kind="instance_group",
        owner_uid=ig_uid,
    )
    assert vpcs, f"no nico VPC owned by instance group uid={ig_uid}"
    vpc = vpcs[0]
    vpc_id = vpc["id"]
    vpc_url = api.region_url(
        api_base, region, f"networking/vpcs/{vpc_id}", project=project
    )
    log.info(f"vpc id={vpc_id}")
    vpc = wait.await_api_state(
        lambda: api.get_json(session, vpc_url),
        "active",
        what=f"vpc {vpc_id}",
        timeout=900,
        interval=10,
        steps=log,
        log_every=2,
    )
    vpc_uid = vpc["uid"]

    log.step("assert VPC has only the platform default security group")
    bound = list(vpc.get("securityGroups") or [])
    assert len(bound) == 1, f"expected only default SG on new VPC, got {bound!r}"
    default_sg_id = bound[0]
    assert default_sg_id.endswith("-default"), (
        f"default SG id shape: want *-default, got {default_sg_id!r}"
    )
    default_sg = api.get_json(session, f"{sg_collection}/{default_sg_id}")
    default_sg_uid = default_sg["uid"]
    assert default_sg.get("ownerKind") == "vpc", (
        f"default SG ownerKind={default_sg.get('ownerKind')!r}, want vpc"
    )
    assert default_sg.get("state") == "active"
    log.ok(f"default SG {default_sg_id!r} (ownerKind=vpc uid={default_sg_uid})")

    if effective_ready:
        log.step("effective read: fresh VPC reports only the platform default")
        eff = _effective("vpc", vpc_id)
        secgroups.assert_effective_object(eff, kind="vpc", obj_id=vpc_id, uid=vpc_uid)
        assert [b.get("id") for b in eff.get("vpcs") or []] == [vpc_id], (
            "a vpc read returns exactly one block, that VPC: "
            f"{[b.get('id') for b in eff.get('vpcs') or []]!r}"
        )
        block = secgroups.effective_block(eff, vpc_id)
        assert secgroups.effective_group_ids(block) == [default_sg_id]
        secgroups.assert_effective_group(
            block, default_sg_id, source="vpc", source_id=vpc_id, sg=default_sg
        )

        eff = _effective("instance_group", ig_id)
        secgroups.assert_effective_object(
            eff, kind="instance_group", obj_id=ig_id, uid=ig_uid
        )
        assert secgroups.effective_group_ids(
            secgroups.effective_block(eff, vpc_id)
        ) == [default_sg_id], (
            "the instance-group binding is still empty, so only the VPC floor applies"
        )
        # A VPC on a backend with no security-group construct must come back as an
        # empty block rather than with the owner's binding merged into it.
        all_vpcs = {
            v["id"]: v
            for v in secgroups.owner_vpcs(
                session,
                api_base,
                region,
                project,
                owner_kind="instance_group",
                owner_uid=ig_uid,
                backend=None,
            )
        }
        secgroups.assert_backend_gate(eff, all_vpcs)
        log.ok(f"{len(eff.get('vpcs') or [])} block(s); non-nico blocks empty")

    log.step(
        f"attach VPC SGs [{vpc_custom_sg_id}, {default_sg_id}] (custom + default)"
    )
    attached = api.set_vpc_security_groups(
        session, vpc_url, [vpc_custom_sg_id, default_sg_id]
    )
    assert list(attached.get("securityGroups") or []) == [
        vpc_custom_sg_id,
        default_sg_id,
    ]
    vpc = wait.await_api_state(
        lambda: api.get_json(session, vpc_url),
        "active",
        what=f"vpc {vpc_id} after SG attach",
        timeout=900,
        interval=10,
        steps=log,
        log_every=2,
    )
    assert list(vpc.get("securityGroups") or []) == [vpc_custom_sg_id, default_sg_id]
    log.info("wait for instance group to settle after VPC re-render")
    wait.await_api_state(
        _get_ig,
        "active",
        what=f"instance group {ig_id} after VPC SG attach",
        timeout=900,
        interval=10,
        steps=log,
        log_every=2,
    )
    log.ok("VPC binding settled")

    kube = k8s.api_client(os.environ.get("KUBECONFIG"))
    ns = k8s.project_namespace(project)

    log.step(f"assert UFO SecurityGroup CRs in {ns}")
    sgs_by_id = {
        vpc_custom_sg_id: secgroups.await_sg_active(
            session, sg_collection, vpc_custom_sg_id
        ),
        default_sg_id: secgroups.await_sg_active(session, sg_collection, default_sg_id),
        ig_sg_id: ig_sg,
    }
    for sg_id in (vpc_custom_sg_id, default_sg_id):
        log.info(f"check UFO CR for {sg_id} → sg-{sgs_by_id[sg_id]['uid']}")
        secgroups.assert_ufo_security_group_cr(
            kube, ns, sgs_by_id[sg_id], api_id=sg_id
        )
    log.ok("VPC-attached UFO SGs present with matching rules")

    if effective_ready:
        log.step("effective read: VPC block matches the binding, rules in write order")
        block = secgroups.effective_block(_effective("vpc", vpc_id), vpc_id)
        assert secgroups.effective_group_ids(block) == [
            vpc_custom_sg_id,
            default_sg_id,
        ], (
            f"a vpc read must report the ids GET /vpcs/{vpc_id} lists, in the same "
            f"order; got {secgroups.effective_group_ids(block)!r}"
        )
        for sg_id in (vpc_custom_sg_id, default_sg_id):
            secgroups.assert_effective_group(
                block, sg_id, source="vpc", source_id=vpc_id, sg=sgs_by_id[sg_id]
            )
        want = secgroups.api_rule_fingerprints(
            sgs_by_id[vpc_custom_sg_id]
        ) | secgroups.api_rule_fingerprints(sgs_by_id[default_sg_id])
        have = secgroups.effective_rule_fingerprints(block)
        assert have == want, (
            f"effective rules for vpc {vpc_id}: missing {want - have!r}, "
            f"unexpected {have - want!r}"
        )
        # First-match evaluation applies within one group's run, so the
        # flattening across groups must not re-sort a group's own rules.
        for direction in ("ingress", "egress"):
            assert secgroups.effective_rule_names(
                block, vpc_custom_sg_id, direction
            ) == [
                r["name"]
                for r in secgroups.sg_api_rules(sgs_by_id[vpc_custom_sg_id])[direction]
            ], f"{direction} order for {vpc_custom_sg_id} lost in the flattening"
        secgroups.assert_effective_self_consistent(block)
        log.ok("effective vpc read agrees with the VPC binding")

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
        custom_fps = secgroups.api_rule_fingerprints(sgs_by_id[vpc_custom_sg_id])
        for item in items:
            status_id = (item.get("status") or {}).get("id") or ""
            if not status_id:
                continue
            have = secgroups.nsg_rule_fingerprints(item)
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

    def _nsg_fresh():
        return k8s.get_custom(
            kube,
            group="nico.mirantis.com",
            version="v1alpha1",
            plural="networksecuritygroups",
            namespace=ns,
            name=nsg_name,
        )

    def _await_effective_matches_nsg(label: str) -> None:
        """The effective read and the rendered NSG must report the same rule set.

        Equality, not containment: the read exists so a tenant does not have to
        reproduce the merge, and a subset assertion is exactly what would let the
        read and the fabric drift apart unnoticed.
        """

        def _pred():
            fresh = _nsg_fresh()
            if not fresh:
                return None
            block = secgroups.effective_block(
                _effective("instance_group", ig_id), vpc_id
            )
            eff_fps = secgroups.effective_rule_fingerprints(block)
            nsg_fps = secgroups.nsg_rule_fingerprints(fresh)
            if eff_fps != nsg_fps:
                return None
            return block

        wait.await_predicate(
            _pred,
            timeout=900,
            interval=10,
            desc=f"effective read and NICo NSG report the same rules ({label})",
            steps=log,
            log_every=2,
        )

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
        fresh = _nsg_fresh()
        if not fresh:
            return None
        have = secgroups.nsg_rule_fingerprints(fresh)
        want = secgroups.api_rule_fingerprints(
            sgs_by_id[vpc_custom_sg_id]
        ) | secgroups.api_rule_fingerprints(sgs_by_id[default_sg_id])
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

    if effective_ready:
        log.step("effective read agrees with the rendered NICo NSG (VPC binding)")
        _await_effective_matches_nsg("VPC binding only")
        log.ok()

    log.step(f"PATCH instance group securityGroups=[{ig_sg_id}]")
    patched = api.set_instance_group_security_groups(session, ig_url, [ig_sg_id])
    assert list(patched.get("securityGroups") or []) == [ig_sg_id]
    wait.await_api_state(
        _get_ig,
        "active",
        what=f"instance group {ig_id} after IG SG attach",
        timeout=900,
        interval=10,
        steps=log,
        log_every=2,
    )
    ig_obj = _get_ig()
    assert list(ig_obj.get("securityGroups") or []) == [ig_sg_id]
    log.ok("instance group binding settled")

    log.step(f"assert UFO CR for instance-group SG {ig_sg_id}")
    sgs_by_id[ig_sg_id] = secgroups.await_sg_active(session, sg_collection, ig_sg_id)
    secgroups.assert_ufo_security_group_cr(
        kube, ns, sgs_by_id[ig_sg_id], api_id=ig_sg_id
    )
    log.ok()

    ig_rule_names = {
        r.get("name")
        for r in secgroups.sg_api_rules(sgs_by_id[ig_sg_id])["ingress"]
        if r.get("name")
    }
    vpc_rule_names = {
        r.get("name")
        for sg_id in (vpc_custom_sg_id, default_sg_id)
        for r in secgroups.sg_api_rules(sgs_by_id[sg_id])["ingress"]
        if r.get("name")
    }
    assert ig_rule_names, "IG SG must have named ingress rules for precedence"
    assert "ig-allow-ssh" in ig_rule_names

    log.step("assert NICo NSG has IG+VPC rules; IG ingress precedes VPC")

    def _nsg_has_ig_and_precedence():
        fresh = _nsg_fresh()
        if not fresh:
            return None
        have = secgroups.nsg_rule_fingerprints(fresh)
        want = (
            secgroups.api_rule_fingerprints(sgs_by_id[ig_sg_id])
            | secgroups.api_rule_fingerprints(sgs_by_id[vpc_custom_sg_id])
            | secgroups.api_rule_fingerprints(sgs_by_id[default_sg_id])
        )
        if not want <= have:
            return None

        order = secgroups.nsg_named_ingress_order(fresh)
        ig_idx = secgroups.first_index(order, ig_rule_names)
        vpc_idx = secgroups.first_index(order, vpc_rule_names)
        if ig_idx is None or vpc_idx is None:
            return None
        if ig_idx >= vpc_idx:
            return None
        return fresh

    nsg_final = wait.await_predicate(
        _nsg_has_ig_and_precedence,
        timeout=900,
        interval=10,
        desc="NICo NSG has IG+VPC rules with IG precedence",
        steps=log,
        log_every=2,
    )
    order = secgroups.nsg_named_ingress_order(nsg_final)
    log.info(f"named ingress order: {order}")
    log.ok("instance-group rules have higher precedence than VPC")

    ig_fps = secgroups.api_rule_fingerprints(sgs_by_id[ig_sg_id])
    vpc_custom_fps = secgroups.api_rule_fingerprints(sgs_by_id[vpc_custom_sg_id])
    default_fps = secgroups.api_rule_fingerprints(sgs_by_id[default_sg_id])

    if effective_ready:
        log.step("effective read: IG binding merges ahead of the VPC floor")
        want_ids = [ig_sg_id, vpc_custom_sg_id, default_sg_id]

        def _effective_merged():
            block = secgroups.effective_block(
                _effective("instance_group", ig_id), vpc_id
            )
            return block if secgroups.effective_group_ids(block) == want_ids else None

        block = wait.await_predicate(
            _effective_merged,
            timeout=900,
            interval=10,
            desc=f"effective instance_group block for {vpc_id} == {want_ids!r}",
            steps=log,
            log_every=2,
        )
        secgroups.assert_effective_group(
            block,
            ig_sg_id,
            source="instance_group",
            source_id=ig_id,
            sg=sgs_by_id[ig_sg_id],
        )
        for sg_id in (vpc_custom_sg_id, default_sg_id):
            secgroups.assert_effective_group(
                block, sg_id, source="vpc", source_id=vpc_id, sg=sgs_by_id[sg_id]
            )
        have = secgroups.effective_rule_fingerprints(block)
        want = ig_fps | vpc_custom_fps | default_fps
        assert have == want, (
            f"merged effective rules: missing {want - have!r}, "
            f"unexpected {have - want!r}"
        )
        secgroups.assert_effective_self_consistent(block)
        log.ok("owner-bound group first, then the VPC floor, deduplicated")

        log.step("effective read: a vpc read does not merge the owner's groups in")
        vpc_block = secgroups.effective_block(_effective("vpc", vpc_id), vpc_id)
        assert secgroups.effective_group_ids(vpc_block) == [
            vpc_custom_sg_id,
            default_sg_id,
        ], (
            "a vpc read reports that VPC's own binding only; the owner's groups "
            f"are read from the owner. Got {secgroups.effective_group_ids(vpc_block)!r}"
        )
        log.ok()

        log.step("effective read agrees with the rendered NICo NSG (both bindings)")
        _await_effective_matches_nsg("instance group + VPC bindings")
        log.ok()

    log.step("detach IG SG and assert its rules leave the NICo NSG")
    try:
        wait.await_api_state(
            _get_ig,
            "active",
            what=f"instance group {ig_id} before IG SG detach",
            timeout=300,
            interval=10,
            steps=log,
            log_every=2,
        )
        api.set_instance_group_security_groups(session, ig_url, [])
        wait.await_api_state(
            _get_ig,
            "active",
            what=f"instance group {ig_id} after IG SG detach",
            timeout=900,
            interval=10,
            steps=log,
            log_every=2,
        )
        assert list(_get_ig().get("securityGroups") or []) == []

        def _nsg_without_ig_rules():
            fresh = _nsg_fresh()
            if not fresh:
                return None
            have = secgroups.nsg_rule_fingerprints(fresh)
            if have & ig_fps:
                return None
            want = vpc_custom_fps | default_fps
            return fresh if want <= have else None

        wait.await_predicate(
            _nsg_without_ig_rules,
            timeout=900,
            interval=10,
            desc="NICo NSG dropped IG SG rules (VPC rules remain)",
            steps=log,
            log_every=2,
        )
        log.ok("ig-sg rules gone from NICo NSG")

        if effective_ready:
            log.step("effective read: IG SG gone from the block")

            def _effective_without_ig_sg():
                block = secgroups.effective_block(
                    _effective("instance_group", ig_id), vpc_id
                )
                if secgroups.effective_group_ids(block) != [
                    vpc_custom_sg_id,
                    default_sg_id,
                ]:
                    return None
                if secgroups.effective_rule_fingerprints(
                    block, security_group_id=ig_sg_id
                ):
                    return None
                return block

            wait.await_predicate(
                _effective_without_ig_sg,
                timeout=900,
                interval=10,
                desc=f"effective instance_group block dropped {ig_sg_id}",
                steps=log,
                log_every=2,
            )
            log.ok()

        log.step("detach VPC custom SG ([] → default only) and assert custom rules leave NICo")
        wait.await_api_state(
            lambda: api.get_json(session, vpc_url),
            "active",
            what=f"vpc {vpc_id} before custom SG detach",
            timeout=900,
            interval=10,
            steps=log,
            log_every=2,
        )
        api.set_vpc_security_groups(session, vpc_url, [])
        wait.await_api_state(
            lambda: api.get_json(session, vpc_url),
            "active",
            what=f"vpc {vpc_id} after custom SG detach",
            timeout=900,
            interval=10,
            steps=log,
            log_every=2,
        )
        wait.await_api_state(
            _get_ig,
            "active",
            what=f"instance group {ig_id} after VPC SG detach",
            timeout=900,
            interval=10,
            steps=log,
            log_every=2,
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
            have = secgroups.nsg_rule_fingerprints(fresh)
            if have & ig_fps:
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

        if effective_ready:
            log.step("effective read: only the VPC default remains")

            def _effective_default_only():
                block = secgroups.effective_block(
                    _effective("instance_group", ig_id), vpc_id
                )
                if secgroups.effective_group_ids(block) != [default_sg_id]:
                    return None
                return (
                    block
                    if secgroups.effective_rule_fingerprints(block) == default_fps
                    else None
                )

            wait.await_predicate(
                _effective_default_only,
                timeout=900,
                interval=10,
                desc=f"effective instance_group block for {vpc_id} == [{default_sg_id}]",
                steps=log,
                log_every=2,
            )
            log.ok()
    finally:
        log.step(f"DELETE instance group {ig_id}")
        deleted = api.delete(session, ig_url)
        assert deleted.status_code in (202, 204), deleted.text
        wait.await_api_absent(
            lambda: None if api.get(session, ig_url).status_code == 404 else True,
            timeout=1800,
            interval=15,
            desc=f"instance group {ig_id} deleted",
            steps=log,
            log_every=2,
        )
        if effective_ready:
            log.step(f"effective read: deleted instance group {ig_id} is 404")
            gone = secgroups.get_effective_raw(
                session,
                api_base,
                region,
                project,
                params={"objectKind": "instance_group", "objectId": ig_id},
            )
            assert gone.status_code == 404, (
                f"effective read of a deleted instance group must be 404, got "
                f"{gone.status_code}: {gone.text[:300]}"
            )
            log.ok()
        if default_sg_id:
            log.step(
                f"assert vpc {vpc_id} and its default SG {default_sg_id!r} are gone"
            )
            wait.await_api_absent(
                lambda: None if api.get(session, vpc_url).status_code == 404 else True,
                timeout=900,
                interval=10,
                desc=f"vpc {vpc_id} deleted with instance group",
                steps=log,
                log_every=2,
            )
            wait.await_api_absent(
                lambda: None
                if api.get(session, f"{sg_collection}/{default_sg_id}").status_code
                == 404
                else True,
                timeout=900,
                interval=10,
                desc=f"vpc default security group {default_sg_id} deleted with vpc",
                steps=log,
                log_every=2,
            )
            if default_sg_uid:
                cr_name = k8s.ufo_security_group_name(default_sg_uid)

                def _ufo_default_sg_gone():
                    return (
                        True
                        if not k8s.get_custom(
                            kube,
                            group="ufo.mirantis.com",
                            version="v1alpha1",
                            plural="securitygroups",
                            namespace=ns,
                            name=cr_name,
                        )
                        else None
                    )

                wait.await_predicate(
                    _ufo_default_sg_gone,
                    timeout=300,
                    interval=5,
                    desc=f"UFO SecurityGroup {cr_name} for vpc default SG gone",
                    steps=log,
                    log_every=2,
                )
            log.ok("vpc default SG removed with vpc")
        log.step(f"DELETE security groups {ig_sg_id!r} and {vpc_custom_sg_id!r}")
        secgroups.delete_security_group(session, sg_collection, ig_sg_id, log=log)
        secgroups.delete_security_group(
            session, sg_collection, vpc_custom_sg_id, log=log
        )
    log.done()
