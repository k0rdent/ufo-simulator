"""Network schema and attachment templates (k0rdent-apis KNF-460) on the lab.

Creates no clusters and finishes in seconds. Upstream's MOCK_MODE suite
(``k0rdent-apis/tests/e2e/compute/tests/test_network_templates_crud.py``) covers
CRUD, refusals and the status lifecycle exhaustively against kind. This file
checks what only a lab can:

  * the lab's global ClusterTypes really reference templates and resolve them
    through Kong. ``ensure_exists`` never replaces an existing object, so a lab
    that still holds the inline ClusterTypes from before the move to templates
    (same ids) fails here, with the fix in the message, instead of silently
    running every provisioning test on inline networking;
  * the template lifecycle against the lab's real ``global-default`` address
    pool and ``nico-lab`` MachineType: draft refused as a reference, in-use
    templates undeletable, deprecation keeping the existing type working;
  * the create-time checks that read lab objects: the address pool's prefix
    window and the MachineType's existence.

Status codes and messages are the ones the upstream suite pins, so the two
suites cannot drift.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from conftest import (
    GLOBAL_PREREQS,
    SCHEMA_TEMPLATES,
    VERITY_PREREQS,
    attachment_templates,
    auth_configured,
    ensure_global_prereqs,
    ensure_verity_prereqs,
    fabric_backend,
    load_template,
)
from helpers import api
from helpers.names import resource_id
from helpers.steps import Steps

pytestmark = [
    pytest.mark.smoke,
    pytest.mark.templates,
    pytest.mark.skipif(not auth_configured(), reason="API_BASE required"),
]

_CLUSTER_TYPES = "compute/cluster-types"
# As upstream: one page holds every template of the region.
_ALL = {"limit": 500}

_NICO_SCHEMA_YAML = "global/network-schema-template-nico.yaml"
_NICO_ATTACHMENT_YAML = "global/network-attachment-template-nico-lab.yaml"
_NICO_TYPE_YAML = "global/cluster-type-nico-hcp.yaml"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _error(resp) -> dict[str, Any]:
    try:
        return (resp.json() or {}).get("error") or {}
    except ValueError:
        return {}


def _reason(resp) -> str | None:
    details = _error(resp).get("details") or [{}]
    return details[0].get("reason")


def _message(resp) -> str:
    return _error(resp).get("message") or resp.text


def _set_status(session, url: str, status: str):
    return session.patch(url, json={"status": status}, timeout=60)


def _listed_ids(session, url: str, key: str, **params: Any) -> set[str]:
    resp = session.get(url, params={**_ALL, **params}, timeout=30)
    api.raise_for_status(resp)
    return {t["id"] for t in resp.json().get(key) or []}


def _link(region: str, resource: str, obj_id: str) -> str:
    return f"/v1/regions/{region}/{resource}/{obj_id}"


def _global_template_yamls(region: str) -> dict[str, tuple[str, dict[str, Any]]]:
    """Template link -> (YAML name, body) for every template the suite ensures."""
    out: dict[str, tuple[str, dict[str, Any]]] = {}
    for name, resource in GLOBAL_PREREQS + VERITY_PREREQS:
        if resource.startswith(SCHEMA_TEMPLATES):
            body = load_template(name)
            out[_link(region, resource, body["id"])] = (name, body)
    return out


def _cluster_type_params() -> list:
    params = [
        pytest.param(name, id=Path(name).stem)
        for name, resource in GLOBAL_PREREQS
        if resource == _CLUSTER_TYPES
    ]
    params += [
        pytest.param(
            name,
            id=Path(name).stem,
            marks=pytest.mark.skipif(
                fabric_backend() != "verity",
                reason="verity labs only (E2E_FABRIC_BACKEND)",
            ),
        )
        for name, resource in VERITY_PREREQS
        if resource == _CLUSTER_TYPES
    ]
    return params


def _assert_global_template(
    obj: dict[str, Any], link: str, yaml_name: str, yaml_body: dict[str, Any], *fields: str
) -> None:
    assert obj.get("selfLink") == link, f"{link}: selfLink={obj.get('selfLink')!r}"
    assert obj.get("status") == "available", (
        f"{link} has status {obj.get('status')!r}: a new ClusterType can reference "
        "only an available template"
    )
    for field in fields:
        assert obj.get(field) == yaml_body[field], (
            f"{link}.{field} differs from {yaml_name}. Network templates cannot "
            "change after create and ensure_exists never replaces them, so the lab "
            "still holds the old content. Delete the ClusterTypes that reference it, "
            "then the attachment template, then the schema template, and rerun "
            "(see e2e/README.md, 'Network templates')."
        )


@pytest.fixture(scope="module")
def globals_ready(session, api_base, region) -> None:
    ensure_global_prereqs(session, api_base, region)
    if fabric_backend() == "verity":
        ensure_verity_prereqs(session, api_base, region)


class _Cleanup:
    """Best-effort delete of what a test created, newest first.

    The tests delete everything as asserted steps; this only catches what a
    failure left behind, and never raises over the original failure.
    """

    def __init__(self, session) -> None:
        self._session = session
        self._urls: list[str] = []

    def add(self, url: str) -> None:
        self._urls.append(url)

    def run(self) -> None:
        for url in reversed(self._urls):
            try:
                resp = api.delete(self._session, url)
            except Exception as exc:  # noqa: BLE001 -- never mask the test's failure
                print(f"cleanup: DELETE {url} raised {exc!r}")
                continue
            if resp.status_code not in (204, 404):
                print(f"cleanup: DELETE {url} returned {resp.status_code}: {resp.text[:300]}")


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ct_yaml", _cluster_type_params())
def test_global_cluster_type_resolves_its_templates(
    session, api_base, region, globals_ready, ct_yaml
):
    """A global ClusterType references its templates, and GET resolves them."""
    want = load_template(ct_yaml)
    ct_id = want["id"]
    log = Steps(f"ClusterType {ct_id} resolves its network templates")
    yamls = _global_template_yamls(region)

    log.step(f"GET {_CLUSTER_TYPES}/{ct_id}: references, not inline networking")
    ct = api.get_json(session, f"{api.region_url(api_base, region, _CLUSTER_TYPES)}/{ct_id}")
    assert ct.get("networkSchemaRef") == want["networkSchemaRef"], (
        f"ClusterType {ct_id} has networkSchemaRef={ct.get('networkSchemaRef')!r}, "
        f"want {want['networkSchemaRef']!r}. The lab still holds the ClusterType "
        "created before the suite moved to network templates, and ensure_exists "
        f"never replaces it: `k0r.sh delete {_CLUSTER_TYPES} {ct_id}` and rerun "
        "(see e2e/README.md, 'Network templates')."
    )
    log.ok(f"networkSchemaRef={ct['networkSchemaRef']['template']}")

    schema_link = want["networkSchemaRef"]["template"]
    schema_name, schema_yaml = yamls[schema_link]
    log.step(f"GET {schema_link}: available, matches {schema_name}, listed")
    schema = api.get_json(session, f"{api_base}{schema_link}")
    _assert_global_template(schema, schema_link, schema_name, schema_yaml, "networkSchema")
    assert ct.get("networkSchema") == schema["networkSchema"], (
        f"ClusterType {ct_id} resolves a networkSchema that differs from {schema_link}"
    )
    schemas_url = api.region_url(api_base, region, SCHEMA_TEMPLATES)
    assert schema["id"] in _listed_ids(session, schemas_url, "networkSchemaTemplates")
    log.ok()

    pools = {p.get("id"): p for p in ct.get("nodePools") or []}
    assert set(pools) == {p["id"] for p in want["nodePools"]}, f"node pools: {sorted(pools)}"
    for want_pool in want["nodePools"]:
        pool = pools[want_pool["id"]]
        link = want_pool["networkAttachmentRef"]["template"]
        att_name, att_yaml = yamls[link]
        log.step(f"pool {pool['id']}: GET {link}: available, matches {att_name}, listed")
        assert pool.get("networkAttachmentRef") == want_pool["networkAttachmentRef"], (
            f"pool {pool['id']}: networkAttachmentRef={pool.get('networkAttachmentRef')!r}"
        )
        att = api.get_json(session, f"{api_base}{link}")
        _assert_global_template(
            att, link, att_name, att_yaml, "networkAttachment", "machineTypes"
        )
        assert att.get("networkSchemaTemplate") == schema_link, (
            f"{link}: parent {att.get('networkSchemaTemplate')!r}, want {schema_link}"
        )
        assert pool.get("machineType") in att["machineTypes"], (
            f"pool {pool['id']}: machineType {pool.get('machineType')!r} is not one "
            f"of {link}'s machineTypes {att['machineTypes']!r}"
        )
        assert pool.get("networkAttachment") == att["networkAttachment"], (
            f"pool {pool['id']} resolves a networkAttachment that differs from {link}"
        )
        nested = api.region_url(api_base, region, attachment_templates(schema["id"]))
        assert att["id"] in _listed_ids(session, nested, "networkAttachmentTemplates")
        log.ok()
    log.done()


def test_network_template_lifecycle(session, api_base, region, run_id, request, globals_ready):
    """draft -> available -> deprecated on templates built from the lab's own
    pool and MachineType, with a ClusterType referencing them in between."""
    log = Steps("network template lifecycle on lab objects")
    log.info(f"run_id={run_id}")

    def rid(kind: str) -> str:
        return resource_id(request.node.name, kind, run_id=run_id)

    schema_id, att_id, ct_id, ct2_id = rid("nst"), rid("nat"), rid("ct"), rid("ct2")
    schemas = api.region_url(api_base, region, SCHEMA_TEMPLATES)
    schema_url = f"{schemas}/{schema_id}"
    nested = api.region_url(api_base, region, attachment_templates(schema_id))
    att_url = f"{nested}/{att_id}"
    cts = api.region_url(api_base, region, _CLUSTER_TYPES)
    ct_url = f"{cts}/{ct_id}"
    schema_link = _link(region, SCHEMA_TEMPLATES, schema_id)
    att_link = _link(region, attachment_templates(schema_id), att_id)

    schema_body = {**load_template(_NICO_SCHEMA_YAML), "id": schema_id}
    schema_body["displayName"] = f"e2e {schema_id}"
    att_body = {**load_template(_NICO_ATTACHMENT_YAML), "id": att_id, "status": "draft"}
    att_body["displayName"] = f"e2e {att_id}"
    type_yaml = load_template(_NICO_TYPE_YAML)

    def type_body(new_id: str) -> dict[str, Any]:
        body = copy.deepcopy(type_yaml)
        body["id"] = new_id
        body["displayName"] = f"e2e templated {new_id}"
        body["networkSchemaRef"] = {"template": schema_link}
        for pool in body["nodePools"]:
            pool["networkAttachmentRef"] = {"template": att_link}
        return body

    cleanup = _Cleanup(session)

    def refused_type(new_id: str, want: str, what: str) -> None:
        resp = session.post(cts, json=type_body(new_id), timeout=60)
        if resp.status_code in (200, 201):
            cleanup.add(f"{cts}/{new_id}")
        assert resp.status_code == 422 and want in _message(resp), (
            f"{what}: {resp.status_code}: {resp.text[:500]}"
        )

    try:
        log.step(f"POST schema template {schema_id} (available, global-default pool)")
        resp = session.post(schemas, json=schema_body, timeout=60)
        api.raise_for_status(resp)
        cleanup.add(schema_url)
        created = resp.json()
        assert created["status"] == "available", created
        assert created["selfLink"] == schema_link, created
        assert created["networkSchema"] == schema_body["networkSchema"], created
        log.ok()

        log.step(f"POST attachment template {att_id} as draft (nico-lab MachineType)")
        resp = session.post(nested, json=att_body, timeout=60)
        api.raise_for_status(resp)
        cleanup.add(att_url)
        created = resp.json()
        assert created["status"] == "draft", created
        assert created["networkSchemaTemplate"] == schema_link, created
        assert created["machineTypes"] == att_body["machineTypes"], created
        log.ok()

        log.step("a ClusterType cannot reference a draft attachment template (422)")
        refused_type(
            ct_id,
            f'network attachment template "{att_id}" has status "draft"',
            "create on a draft attachment template",
        )
        log.ok()

        log.step(f"PATCH {att_id} -> available, then create ClusterType {ct_id}")
        resp = _set_status(session, att_url, "available")
        assert resp.status_code == 200 and resp.json()["status"] == "available", (
            f"{resp.status_code}: {resp.text[:500]}"
        )
        resp = session.post(cts, json=type_body(ct_id), timeout=60)
        if resp.status_code in (200, 201):
            cleanup.add(ct_url)
        assert resp.status_code == 201, f"{resp.status_code}: {resp.text[:500]}"
        log.ok()

        def assert_type_resolves(what: str) -> None:
            ct = api.get_json(session, ct_url)
            assert ct.get("networkSchemaRef") == {"template": schema_link}, (what, ct)
            assert ct.get("networkSchema") == schema_body["networkSchema"], (what, ct)
            pool = ct["nodePools"][0]
            assert pool.get("networkAttachmentRef") == {"template": att_link}, (what, pool)
            assert pool.get("networkAttachment") == att_body["networkAttachment"], (what, pool)

        log.step(f"GET {ct_id}: references plus the templates' networking")
        assert_type_resolves("after create")
        log.ok()

        log.step("templates in use are not deletable (409 CONFLICT_IN_USE)")
        for url in (att_url, schema_url):
            resp = api.delete(session, url)
            assert resp.status_code == 409 and _reason(resp) == "CONFLICT_IN_USE", (
                f"DELETE {url}: {resp.status_code}: {resp.text[:500]}"
            )
        log.ok()

        log.step("deprecate both: the existing type still resolves, lists hide them")
        for url in (schema_url, att_url):
            resp = _set_status(session, url, "deprecated")
            assert resp.status_code == 200 and resp.json()["status"] == "deprecated", (
                f"PATCH {url}: {resp.status_code}: {resp.text[:500]}"
            )
        assert_type_resolves("after deprecation")
        assert schema_id not in _listed_ids(session, schemas, "networkSchemaTemplates")
        assert schema_id in _listed_ids(
            session, schemas, "networkSchemaTemplates", includeDeprecated="true"
        )
        assert att_id not in _listed_ids(session, nested, "networkAttachmentTemplates")
        assert att_id in _listed_ids(
            session, nested, "networkAttachmentTemplates", includeDeprecated="true"
        )
        log.ok()

        log.step("deprecation is forward only and refuses new references (422)")
        resp = _set_status(session, schema_url, "available")
        assert resp.status_code == 422, f"backward move: {resp.status_code}: {resp.text[:500]}"
        refused_type(ct2_id, 'has status "deprecated"', "create on deprecated templates")
        log.ok()

        log.step("delete type -> attachment -> schema; each goes once unreferenced")
        for url in (ct_url, att_url, schema_url):
            resp = api.delete(session, url)
            assert resp.status_code == 204, f"DELETE {url}: {resp.status_code}: {resp.text[:500]}"
            gone = api.get(session, url)
            assert gone.status_code == 404, f"GET {url} after delete: {gone.status_code}"
        log.ok()
        log.done()
    finally:
        cleanup.run()


def test_network_template_create_checks_lab_objects(
    session, api_base, region, run_id, request, globals_ready
):
    """The create-time checks that read lab objects: the address pool's prefix
    window and the MachineType's existence."""
    log = Steps("network template create checks against lab objects")

    def rid(kind: str) -> str:
        return resource_id(request.node.name, kind, run_id=run_id)

    schemas = api.region_url(api_base, region, SCHEMA_TEMPLATES)
    schema_yaml = load_template(_NICO_SCHEMA_YAML)
    att_yaml = load_template(_NICO_ATTACHMENT_YAML)
    pool_link = schema_yaml["networkSchema"]["subnets"][0]["addressPool"]
    pool = api.get_json(session, f"{api_base}{pool_link}")
    cleanup = _Cleanup(session)

    try:
        too_long = pool["maxPrefixLength"] + 2
        bad_id = rid("window")
        log.step(f"prefixLength {too_long} outside {pool['id']}'s window -> 422")
        bad = copy.deepcopy(schema_yaml)
        bad["id"] = bad_id
        bad["networkSchema"]["subnets"][0]["prefixLength"] = too_long
        resp = session.post(schemas, json=bad, timeout=60)
        if resp.status_code in (200, 201):
            cleanup.add(f"{schemas}/{bad_id}")
        assert resp.status_code == 422 and "allocatable window" in resp.text, (
            f"{resp.status_code}: {resp.text[:500]}"
        )
        log.ok()

        schema_id = rid("nst")
        log.step("attachment template naming a MachineType that does not exist -> 422")
        resp = session.post(schemas, json={**schema_yaml, "id": schema_id}, timeout=60)
        api.raise_for_status(resp)
        cleanup.add(f"{schemas}/{schema_id}")
        nested = api.region_url(api_base, region, attachment_templates(schema_id))
        att_id = rid("nat")
        ghost = _link(region, "infrastructure/machine-types", rid("ghost-mt"))
        resp = session.post(
            nested, json={**att_yaml, "id": att_id, "machineTypes": [ghost]}, timeout=60
        )
        if resp.status_code in (200, 201):
            cleanup.add(f"{nested}/{att_id}")
        assert resp.status_code == 422 and _reason(resp) == "INVALID_ARGUMENT", (
            f"{resp.status_code}: {resp.text[:500]}"
        )
        log.ok()

        log.step(f"delete schema template {schema_id}")
        resp = api.delete(session, f"{schemas}/{schema_id}")
        assert resp.status_code == 204, f"{resp.status_code}: {resp.text[:500]}"
        log.ok()
        log.done()
    finally:
        cleanup.run()
