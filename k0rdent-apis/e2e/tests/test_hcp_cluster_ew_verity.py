"""Smoke: HCP cluster with Verity Spectrum-X east-west fabric + rail NICs.

The Verity sibling of ``test_hcp_cluster_ew``. Same shape, different proof: the
netris backend creates a LinkAttachment per port, while UFO's verity backend on
a Spectrum-X fabric creates one HGXTenantAssignment per host and lets the fabric
controller address the rails. See ``helpers/ew_verity``.

Two nodes rather than one, so there is a cross-node path — which also means this
test holds two lab machines for its duration.
"""

from __future__ import annotations

import os

import pytest

from conftest import (
    auth_configured,
    ensure_global_prereqs,
    ensure_verity_prereqs,
    fabric_backend,
    load_scenario_template,
    verity_backend_name,
)
from helpers import api, ew_verity, k8s, secgroups, wait
from helpers.names import resource_id, stamp_id
from helpers.steps import Steps


pytestmark = [pytest.mark.smoke, pytest.mark.hcp]

_SCENARIO = "hcp_cluster_ew_verity"
# nodePools[].nodeCount in the scenario. Asserted against what the API returns
# rather than trusted, so the two cannot drift apart silently.
_EXPECTED_MACHINES = 2


@pytest.mark.skipif(
    not auth_configured(),
    reason="API_BASE required",
)
@pytest.mark.skipif(
    fabric_backend() != "verity",
    reason="verity lab required (E2E_FABRIC_BACKEND)",
)
# pytest.ini sets a global 1800s wall-clock kill. The verity path is
# structurally slower than netris: the HGXTenantAssignment cannot go ready until
# UFO has created the verity Tenant, the Server has published su-id/host-id from
# LLDP-derived Links, and verity-operator has PATCHed the switchpoint against the
# live API — and its reconciler polls for the Tenant on a flat 30s requeue
# without watching it.
@pytest.mark.timeout(3600)
def test_hcp_cluster_ew_verity_create_ready_terminate(
    session, api_base, region, project, run_id, request
):
    """Ensure globals, create Verity E/W HCP cluster, assert fabric, delete."""
    log = Steps("HCP Verity E/W cluster create → ready → terminate")
    log.info(f"run_id={run_id}")

    log.step("ensure global prereqs (address-pools + cluster-types; never deleted)")
    ensure_global_prereqs(session, api_base, region)
    ensure_verity_prereqs(session, api_base, region)
    log.ok()

    cluster_id = resource_id(request.node.name, "cluster", run_id=run_id)
    cluster = stamp_id(load_scenario_template(_SCENARIO, "cluster.yaml"), cluster_id)
    clusters_url = api.region_url(api_base, region, "compute/clusters", project=project)
    cluster_url = f"{clusters_url}/{cluster_id}"

    kube = k8s.api_client(os.environ.get("KUBECONFIG"))
    ns = k8s.project_namespace(project)

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
        api.raise_for_status(existing)
    else:
        log.info("no leftover cluster")
    log.ok()

    # Assignments are named per physical host, so a leak from an earlier run
    # blocks this one no matter how clean the API side looks.
    ew_verity.assert_no_stuck_hgx(kube, ns, log)

    log.step(f"POST create cluster {cluster_id}")
    created = session.post(clusters_url, json=cluster, timeout=60)
    assert created.status_code in (200, 201), created.text
    log.ok(f"create accepted ({created.status_code})")

    def _get_cluster():
        resp = api.get(session, cluster_url)
        api.raise_for_status(resp)
        return resp.json()

    try:
        log.step("wait for cluster API state=active")
        cluster_obj = wait.await_api_state(
            _get_cluster,
            "active",
            what=f"cluster {cluster_id}",
            timeout=1800,
            interval=15,
            steps=log,
            log_every=2,
        )
        cluster_uid = cluster_obj["uid"]
        log.info(f"cluster uid={cluster_uid}")

        def _cd_ready():
            cd = k8s.find_cluster_deployment(kube, ns, slug=cluster_id, uid=cluster_uid)
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
        cd_name = cd["metadata"]["name"]
        log.info(f"ClusterDeployment {cd_name} Ready")

        backend = verity_backend_name()
        log.step(f"assert cluster owns a {backend} east-west VPC")
        verity_vpcs = secgroups.owner_vpcs(
            session,
            api_base,
            region,
            project,
            owner_kind="cluster",
            owner_uid=cluster_uid,
            backend=backend,
        )
        assert verity_vpcs, (
            f"expected at least one {backend} VPC owned by cluster "
            f"uid={cluster_uid}. If the cluster type names a verity backend "
            "instance without site/fabric_type, UFO takes the Clos path and no "
            "Spectrum-X attachment happens."
        )
        log.ok(
            f"{len(verity_vpcs)} {backend} VPC(s): "
            + ", ".join(
                f"{v.get('id')}(ufoCrName={v.get('ufoCrName')!r})" for v in verity_vpcs
            )
        )

        ew_verity.await_cluster_ew_verity_ready(
            kube,
            ns,
            cluster_deployment_name=cd_name,
            log=log,
            expected_machines=_EXPECTED_MACHINES,
        )
    finally:
        # Always give the machines back. This test holds two of them, and a
        # leaked cluster leaves HGXTenantAssignments that block the next run
        # rather than merely starving it.
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
