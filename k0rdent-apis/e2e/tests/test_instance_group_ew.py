"""Smoke: instance group with Netris east-west fabric + rail NICs.

Uses the same ``netris-hcp-ew`` cluster type as ``test_hcp_cluster_ew`` so the
IG path exercises the same EW networkSchema / eth-ew* attachment layout.
"""

from __future__ import annotations

import os

import pytest

from conftest import (
    auth_configured,
    ensure_global_prereqs,
    fabric_backend,
    load_scenario_template,
)
from helpers import api, ew_netris, k8s, secgroups, wait
from helpers.names import resource_id, stamp_id
from helpers.steps import Steps


pytestmark = [pytest.mark.smoke, pytest.mark.bmaas]

_SCENARIO = "instance_group_ew"


@pytest.mark.skipif(
    not auth_configured(),
    reason="API_BASE required",
)
@pytest.mark.skipif(
    fabric_backend() != "netris",
    reason="netris lab required (E2E_FABRIC_BACKEND)",
)
def test_instance_group_ew_create_active_terminate(
    session, api_base, region, project, run_id, request
):
    """Ensure globals, create Netris E/W IG, assert EW fabric, delete."""
    log = Steps("Instance group E/W create → active → terminate")
    log.info(f"run_id={run_id}")

    log.step("ensure global prereqs (address-pools + cluster-types; never deleted)")
    ensure_global_prereqs(session, api_base, region)
    log.ok()

    ig_id = resource_id(request.node.name, "ig", run_id=run_id)
    ig = stamp_id(load_scenario_template(_SCENARIO, "instance-group.yaml"), ig_id)
    groups_url = api.region_url(
        api_base, region, "compute/instance-groups", project=project
    )
    ig_url = f"{groups_url}/{ig_id}"

    log.step(f"ensure clean slate for instance group {ig_id}")
    existing = api.get(session, ig_url)
    if existing.status_code == 200:
        log.info("leftover instance group present — deleting")
        api.delete(session, ig_url)
        wait.await_api_absent(
            lambda: None if api.get(session, ig_url).status_code == 404 else True,
            timeout=1800,
            interval=15,
            desc=f"instance group {ig_id} gone before recreate",
            steps=log,
            log_every=2,
        )
    elif existing.status_code != 404:
        api.raise_for_status(existing)
    else:
        log.info("no leftover instance group")
    log.ok()

    log.step(f"POST create instance group {ig_id}")
    created = session.post(groups_url, json=ig, timeout=60)
    assert created.status_code in (200, 201), created.text
    log.ok(f"create accepted ({created.status_code})")

    def _get_ig():
        resp = api.get(session, ig_url)
        api.raise_for_status(resp)
        return resp.json()

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

    kube = k8s.api_client(os.environ.get("KUBECONFIG"))
    ns = k8s.project_namespace(project)

    log.step("assert instance group owns a netris east-west VPC")
    netris_vpcs = secgroups.owner_vpcs(
        session,
        api_base,
        region,
        project,
        owner_kind="instance_group",
        owner_uid=ig_uid,
        backend=ew_netris.NETRIS_BACKEND,
    )
    assert netris_vpcs, (
        f"expected at least one netris VPC owned by instance group uid={ig_uid}"
    )
    log.ok(
        f"{len(netris_vpcs)} netris VPC(s): "
        + ", ".join(
            f"{v.get('id')}(ufoCrName={v.get('ufoCrName')!r})" for v in netris_vpcs
        )
    )
    ew_netris.await_instance_group_ew_netris_ready(
        kube,
        ns,
        instance_group_uid=ig_uid,
        log=log,
    )

    log.step(f"DELETE instance group {ig_id}")
    deleted = api.delete(session, ig_url)
    assert deleted.status_code in (202, 204), deleted.text
    log.ok(f"delete accepted ({deleted.status_code})")

    log.step("wait for instance group gone")
    wait.await_api_absent(
        lambda: None if api.get(session, ig_url).status_code == 404 else True,
        timeout=1800,
        interval=15,
        desc=f"instance group {ig_id} deleted",
        steps=log,
        log_every=2,
    )
    log.done()
