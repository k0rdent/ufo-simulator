"""Smoke: ensure prereqs, create HCP cluster, wait ready, terminate."""

from __future__ import annotations

import os

import pytest

from conftest import (
    auth_configured,
    ensure_global_prereqs,
    load_scenario_template,
)
from helpers import api, k8s, wait
from helpers.names import resource_id, stamp_id
from helpers.steps import Steps


pytestmark = [pytest.mark.smoke, pytest.mark.hcp]

_SCENARIO = "hcp_cluster"


@pytest.mark.skipif(
    not auth_configured(),
    reason="API_BASE required",
)
def test_hcp_cluster_create_ready_terminate(
    session, api_base, region, project, run_id, request
):
    """Ensure globals, create cluster, wait ready, delete."""
    log = Steps("HCP cluster create → ready → terminate")
    log.info(f"run_id={run_id}")

    log.step("ensure global prereqs (address-pools + cluster-type; never deleted)")
    ensure_global_prereqs(session, api_base, region)
    log.ok()

    cluster_id = resource_id(request.node.name, "cluster", run_id=run_id)
    cluster = stamp_id(load_scenario_template(_SCENARIO, "cluster.yaml"), cluster_id)
    clusters_url = api.region_url(api_base, region, "compute/clusters", project=project)
    cluster_url = f"{clusters_url}/{cluster_id}"

    log.step(f"ensure clean slate for cluster {cluster_id}")
    existing = api.get(session, cluster_url)
    if existing.status_code == 200:
        log.info("leftover cluster present — deleting")
        api.delete(session, cluster_url)
        wait.await_api_absent(
            lambda: None if api.get(session, cluster_url).status_code == 404 else True,
            timeout=1800,
            interval=15,
            desc=f"cluster {cluster_id} gone before recreate",
            steps=log,
            log_every=2,
        )
    elif existing.status_code != 404:
        existing.raise_for_status()
    else:
        log.info("no leftover cluster")
    log.ok()

    log.step(f"POST create cluster {cluster_id}")
    created = session.post(clusters_url, json=cluster, timeout=60)
    assert created.status_code in (200, 201), created.text
    log.ok(f"create accepted ({created.status_code})")

    def _get_cluster():
        resp = api.get(session, cluster_url)
        resp.raise_for_status()
        return resp.json()

    log.step("wait for cluster API state=active")
    cluster_obj = wait.await_api_state(
        _get_cluster, "active", timeout=1800, interval=15, steps=log, log_every=2
    )
    cluster_uid = cluster_obj["uid"]
    log.info(f"cluster uid={cluster_uid}")

    kube = k8s.api_client(os.environ.get("KUBECONFIG"))
    ns = k8s.project_namespace(project)

    def _cd_ready():
        cd = k8s.find_cluster_deployment(
            kube, ns, slug=cluster_id, uid=cluster_uid
        )
        if not cd:
            return None
        if not k8s.cluster_deployment_ready(cd):
            return None
        return cd

    log.step(f"wait for ClusterDeployment Ready in {ns} (slug={cluster_id})")
    cd = wait.await_predicate(
        _cd_ready,
        timeout=1800,
        interval=15,
        desc="ClusterDeployment Ready",
        steps=log,
        log_every=2,
    )
    log.info(f"ClusterDeployment {cd['metadata']['name']} Ready")

    log.step(f"DELETE cluster {cluster_id}")
    deleted = api.delete(session, cluster_url)
    assert deleted.status_code in (202, 204), deleted.text
    log.ok(f"delete accepted ({deleted.status_code})")

    log.step("wait for cluster gone")
    wait.await_api_absent(
        lambda: None if api.get(session, cluster_url).status_code == 404 else True,
        timeout=1800,
        interval=15,
        desc=f"cluster {cluster_id} deleted",
        steps=log,
        log_every=2,
    )
    log.done()
