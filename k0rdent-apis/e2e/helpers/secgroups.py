"""Shared security-group helpers for the lab e2e scenarios.

Both SG scenarios — HCP cluster and instance group — drive the same shape: create
tenant groups, attach them at the VPC and at the owner, then assert the same rules
reach four surfaces:

* the API security-group resource (``rules.ingress`` / ``rules.egress``),
* the UFO ``SecurityGroup`` CR (``spec.rules``),
* the NICo ``NetworkSecurityGroup`` CR (``spec.rules[]``, each carrying its own
  ``direction``),
* the ``security-groups-effective`` read (``securityGroupRules.ingress|egress``,
  each rule carrying ``securityGroupId``).

Those four spell a rule four different ways, so comparing them at all needs one
vocabulary — ``rule_fingerprint``. It lives here rather than in each test because
a second copy that drifts would silently compare nothing.
"""

from __future__ import annotations

from typing import Any

from helpers import api, k8s, wait
from helpers.names import stamp_id
from helpers.steps import Steps

#: The only fabric backend with a security-group construct behind it. A VPC on
#: any other backend cannot enforce a binding, which both the effective read and
#: the VPC filtering below depend on.
NICO_BACKEND = "nico"

#: Collection path of the effective read, relative to the project scope.
EFFECTIVE_RESOURCE = "networking/security-groups-effective"


# ---------------------------------------------------------------------------
# Security-group lifecycle
# ---------------------------------------------------------------------------


def await_sg_active(session, sg_collection: str, sg_id: str) -> dict[str, Any]:
    return wait.await_api_state(
        lambda: api.get_json(session, f"{sg_collection}/{sg_id}"),
        "active",
        what=f"security group {sg_id}",
        timeout=300,
        interval=5,
    )


def ensure_security_group(
    session, sg_collection: str, scenario: str, template_name: str, sg_id: str
) -> dict[str, Any]:
    from conftest import load_scenario_template

    api.ensure_exists(
        session,
        sg_collection,
        stamp_id(load_scenario_template(scenario, template_name), sg_id),
    )
    return await_sg_active(session, sg_collection, sg_id)


def delete_security_group(
    session, sg_collection: str, sg_id: str, *, log: Steps
) -> None:
    url = f"{sg_collection}/{sg_id}"
    deleted = api.delete(session, url)
    assert deleted.status_code in (204, 404), deleted.text
    wait.await_api_absent(
        lambda: None if api.get(session, url).status_code == 404 else True,
        timeout=300,
        interval=5,
        desc=f"security group {sg_id} deleted",
        steps=log,
        log_every=2,
    )


def fresh_resource(
    session, collection_url: str, body: dict[str, Any], *, what: str
) -> None:
    """Delete a leftover of the same id, then POST the body. ``what`` names the
    kind in the wait description (``cluster``, ``instance group``)."""
    url = f"{collection_url}/{body['id']}"
    existing = api.get(session, url)
    if existing.status_code == 200:
        api.delete(session, url)
        wait.await_api_absent(
            lambda: None if api.get(session, url).status_code == 404 else True,
            timeout=1800,
            interval=15,
            desc=f"{what} {body['id']} gone before recreate",
        )
    elif existing.status_code != 404:
        existing.raise_for_status()

    created = session.post(collection_url, json=body, timeout=60)
    assert created.status_code in (200, 201), created.text


def owner_vpcs(
    session,
    api_base: str,
    region: str,
    project: str,
    *,
    owner_kind: str,
    owner_uid: str,
    backend: str | None = NICO_BACKEND,
) -> list[dict[str, Any]]:
    """VPCs an owner materialized. ``backend=None`` returns every one of them,
    which is what the effective read's per-VPC blocks have to be checked against."""
    vpcs_url = api.region_url(api_base, region, "networking/vpcs", project=project)
    items = api.list_items(session, vpcs_url)
    return [
        v
        for v in items
        if v.get("ownerKind") == owner_kind
        and v.get("ownerId") == owner_uid
        and (backend is None or (v.get("backend") or "").lower() == backend)
    ]


# ---------------------------------------------------------------------------
# One rule vocabulary across API / UFO CR / NICo NSG / effective read
# ---------------------------------------------------------------------------


def sg_api_rules(sg: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    rules = sg.get("rules") or {}
    return {
        "ingress": list(rules.get("ingress") or []),
        "egress": list(rules.get("egress") or []),
    }


def rule_fingerprint(rule: dict[str, Any], *, direction: str | None = None) -> tuple:
    """Compare API / UFO / NICo / effective rules ignoring case on protocol/action."""
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


def api_rule_fingerprints(sg: dict[str, Any]) -> set[tuple]:
    out: set[tuple] = set()
    rules = sg_api_rules(sg)
    for direction, items in rules.items():
        for rule in items:
            out.add(rule_fingerprint(rule, direction=direction))
    return out


def ufo_cr_rule_fingerprints(cr: dict[str, Any]) -> set[tuple]:
    spec = cr.get("spec") or {}
    rules = spec.get("rules") or {}
    out: set[tuple] = set()
    for direction in ("ingress", "egress"):
        for rule in rules.get(direction) or []:
            out.add(rule_fingerprint(rule, direction=direction))
    return out


def nsg_rule_fingerprints(nsg: dict[str, Any]) -> set[tuple]:
    """Flatten NICo NetworkSecurityGroup.spec.rules into comparable fingerprints."""
    out: set[tuple] = set()
    for rule in (nsg.get("spec") or {}).get("rules") or []:
        direction = (rule.get("direction") or "").lower()
        name = rule.get("name") or ""
        if name.startswith("ufo-default-"):
            continue
        out.add(rule_fingerprint(rule, direction=direction))
    return out


def assert_rules_present(haystack: set[tuple], needle: set[tuple], *, label: str) -> None:
    missing = needle - haystack
    assert not missing, f"{label}: missing rules {missing!r}; have {haystack!r}"


def assert_ufo_security_group_cr(
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
    assert_rules_present(
        ufo_cr_rule_fingerprints(cr),
        api_rule_fingerprints(sg),
        label=f"UFO CR {cr_name} rules vs API {api_id}",
    )


def nsg_named_ingress_order(nsg: dict[str, Any]) -> list[str]:
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


def first_index(names: list[str], candidates: set[str]) -> int | None:
    for i, name in enumerate(names):
        if name in candidates:
            return i
    return None


# ---------------------------------------------------------------------------
# security-groups-effective (KNF-469)
# ---------------------------------------------------------------------------


def effective_url(api_base: str, region: str, project: str) -> str:
    return api.region_url(api_base, region, EFFECTIVE_RESOURCE, project=project)


def get_effective_raw(
    session, api_base: str, region: str, project: str, *, params: dict[str, Any]
):
    """GET the effective read, returning the raw response.

    ``api.region_url`` cannot express a query string, so the two required
    parameters go through ``params=`` the way ``api.list_items`` already does.
    """
    return session.get(
        effective_url(api_base, region, project), params=params, timeout=30
    )


def get_effective(
    session, api_base: str, region: str, project: str, *, kind: str, object_id: str
) -> dict[str, Any]:
    resp = get_effective_raw(
        session,
        api_base,
        region,
        project,
        params={"objectKind": kind, "objectId": object_id},
    )
    resp.raise_for_status()
    return resp.json()


def effective_endpoint_available(
    session, api_base: str, region: str, project: str
) -> bool:
    """True when this lab runs a k0rdent-apis build carrying KNF-469.

    Probed with NO query params on purpose: both are required, so the endpoint
    answers 422 when it exists. A 404 therefore means the route is not registered
    rather than that some object is missing — which a probe naming an object
    could not tell apart. Any other status is raised rather than read as
    "not deployed": a 5xx here is a real failure.
    """
    resp = get_effective_raw(session, api_base, region, project, params={})
    if resp.status_code == 422:
        return True
    if resp.status_code == 404:
        return False
    resp.raise_for_status()
    raise AssertionError(
        f"probe of {EFFECTIVE_RESOURCE} with no params returned {resp.status_code}, "
        f"want 422 (deployed) or 404 (absent): {resp.text[:300]}"
    )


def assert_effective_object(resp: dict[str, Any], *, kind: str, obj_id: str, uid: str) -> None:
    """The echoed object makes a response self-describing once detached from its request."""
    obj = resp.get("object") or {}
    assert obj.get("kind") == kind, f"object.kind={obj.get('kind')!r}, want {kind!r}"
    assert obj.get("id") == obj_id, f"object.id={obj.get('id')!r}, want {obj_id!r}"
    assert obj.get("uid") == uid, f"object.uid={obj.get('uid')!r}, want {uid!r}"


def effective_block(resp: dict[str, Any], vpc_id: str) -> dict[str, Any]:
    for block in resp.get("vpcs") or []:
        if block.get("id") == vpc_id:
            return block
    raise AssertionError(
        f"no block for vpc {vpc_id!r} in {[b.get('id') for b in (resp.get('vpcs') or [])]!r}"
    )


def effective_group_ids(block: dict[str, Any]) -> list[str]:
    """Group ids in wire order — the MergeSecurityGroups order: owner-bound first,
    then this VPC's own, deduplicated."""
    return [g.get("id") for g in block.get("securityGroups") or []]


def effective_group(block: dict[str, Any], sg_id: str) -> dict[str, Any]:
    for group in block.get("securityGroups") or []:
        if group.get("id") == sg_id:
            return group
    raise AssertionError(
        f"group {sg_id!r} not in block {block.get('id')!r}: {effective_group_ids(block)!r}"
    )


def effective_rule_fingerprints(
    block: dict[str, Any], *, security_group_id: str | None = None
) -> set[tuple]:
    """The block's rules as ``rule_fingerprint`` tuples; direction comes from the
    nest, as it does everywhere else."""
    out: set[tuple] = set()
    rules = block.get("securityGroupRules") or {}
    for direction in ("ingress", "egress"):
        for rule in rules.get(direction) or []:
            if security_group_id and rule.get("securityGroupId") != security_group_id:
                continue
            out.add(rule_fingerprint(rule, direction=direction))
    return out


def effective_rule_names(block: dict[str, Any], sg_id: str, direction: str) -> list[str]:
    """One group's rule names, in wire order — the assertion for "flattening did
    not re-sort a group's run", which first-match evaluation makes load-bearing."""
    rules = (block.get("securityGroupRules") or {}).get(direction) or []
    return [r.get("name") for r in rules if r.get("securityGroupId") == sg_id]


def assert_effective_group(
    block: dict[str, Any],
    sg_id: str,
    *,
    source: str,
    source_id: str,
    sg: dict[str, Any],
) -> None:
    """Identity + attribution of one entry, checked against the group resource."""
    group = effective_group(block, sg_id)
    assert group.get("source") == source and group.get("sourceId") == source_id, (
        f"group {sg_id!r} attributed to "
        f"{group.get('source')!r}/{group.get('sourceId')!r}, want {source!r}/{source_id!r}"
    )
    assert group.get("uid") == sg.get("uid"), (
        f"group {sg_id!r} uid={group.get('uid')!r}, want {sg.get('uid')!r}"
    )
    rules = sg_api_rules(sg)
    want_count = len(rules["ingress"]) + len(rules["egress"])
    assert group.get("ruleCount") == want_count, (
        f"group {sg_id!r} ruleCount={group.get('ruleCount')!r}, "
        f"want {want_count} (ingress+egress on the group resource)"
    )
    # The entry is deliberately identity + attribution only: displayName and
    # state were dropped so there is no second copy of the group to go stale.
    for absent in ("displayName", "state", "ownerKind", "ownerId"):
        assert absent not in group, (
            f"group {sg_id!r} carries {absent!r}; the effective entry must not "
            f"copy the group resource: {group!r}"
        )


def assert_effective_self_consistent(block: dict[str, Any]) -> None:
    """Every rule names a group present in the same block, or the two lists cannot
    be joined."""
    present = set(effective_group_ids(block))
    rules = block.get("securityGroupRules") or {}
    for direction in ("ingress", "egress"):
        for rule in rules.get(direction) or []:
            assert rule.get("securityGroupId") in present, (
                f"{direction} rule {rule.get('name')!r} names group "
                f"{rule.get('securityGroupId')!r}, absent from block "
                f"{block.get('id')!r} securityGroups {sorted(present)!r}"
            )


def assert_backend_gate(resp: dict[str, Any], vpcs_by_id: dict[str, dict[str, Any]]) -> None:
    """A VPC whose backend has no security-group construct filters nothing, so its
    block is empty rather than carrying the owner's binding merged in."""
    for block in resp.get("vpcs") or []:
        vpc = vpcs_by_id.get(block.get("id"))
        if vpc is None:
            continue
        if (vpc.get("backend") or "").lower() == NICO_BACKEND:
            continue
        assert not block.get("securityGroups"), (
            f"vpc {block.get('id')!r} on backend {vpc.get('backend')!r} cannot "
            f"enforce a binding, yet reports {effective_group_ids(block)!r}"
        )
        rules = block.get("securityGroupRules") or {}
        assert not rules.get("ingress") and not rules.get("egress"), (
            f"vpc {block.get('id')!r} on backend {vpc.get('backend')!r} reports "
            f"rules: {rules!r}"
        )
