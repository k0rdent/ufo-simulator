"""Smoke: ensure prereqs, create instance group, wait active, terminate."""

from __future__ import annotations

import pytest

from conftest import (
    auth_configured,
    ensure_global_prereqs,
    load_scenario_template,
)
from helpers import api, wait
from helpers.names import resource_id, stamp_id
from helpers.steps import Steps


pytestmark = [pytest.mark.smoke, pytest.mark.bmaas]

_SCENARIO = "instance_group"


@pytest.mark.skipif(
    not auth_configured(),
    reason="API_BASE required",
)
def test_instance_group_create_active_terminate(
    session, api_base, region, project, run_id, request
):
    """Ensure globals, create instance group, wait API active, delete."""
    log = Steps("Instance group create → active → terminate")
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
        existing.raise_for_status()
    else:
        log.info("no leftover instance group")
    log.ok()

    log.step(f"POST create instance group {ig_id}")
    created = session.post(groups_url, json=ig, timeout=60)
    assert created.status_code in (200, 201), created.text
    log.ok(f"create accepted ({created.status_code})")

    def _get_ig():
        resp = api.get(session, ig_url)
        resp.raise_for_status()
        return resp.json()

    log.step("wait for instance group API state=active")
    ig_obj = wait.await_api_state(
        _get_ig, "active", timeout=1800, interval=15, steps=log, log_every=2
    )
    log.info(f"instance group uid={ig_obj['uid']}")

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
