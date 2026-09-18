"""VPC peering across projects/orgs: cluster-VPC <-> instance-group-VPC.

Identical in flow to the intra-project test — same scenario, both teardown
rounds — and differing only in which project the instance group goes in. See
``helpers.vpc_peering`` for the rounds.

MOCK BOUNDARY. Requires MOCK_MODE **off** (``lab-inject.sh mock off``). Under
mock, VPCPeeringCreate returns before applying a UFO VpcPeering CR, so every CR
assertion here fails. See ``helpers.vpc_peering`` for the shared handshake and
lab prerequisites (two available ``nico-lab`` servers).

Not part of the default ``-m smoke`` run: it provisions a second owner in
``E2E_PEER_PROJECT``.
"""

from __future__ import annotations

import pytest

from conftest import auth_configured
from helpers.steps import Steps
from helpers.vpc_peering import peer_cluster_with_instance_group, require_peer_project

pytestmark = [pytest.mark.peering]


@pytest.mark.crossorg
@pytest.mark.skipif(not auth_configured(), reason="API_BASE required")
def test_vpc_peering_cross_org(
    session, api_base, region, project, peer_project, run_id, request
):
    """Peer across two orgs: kind-main (org kind) <-> acme-main (org acme).

    Cross-org, cross-project and cross-namespace at once. The peering service
    has no org awareness whatsoever, so the expectation is that this behaves
    exactly like the same-project case; this pins it. The one visible
    difference is spec.remote.namespace, which is set only when the projects
    differ — asserted by the shared handshake.

    Both teardown rounds run here exactly as they do same-project; see
    ``helpers.vpc_peering``.
    """
    log = Steps("VPC peering: cross-org (inter-project)")
    log.info(f"run_id={run_id} local={project} remote={peer_project}")

    require_peer_project(session, api_base, region, project, peer_project)

    peer_cluster_with_instance_group(
        session,
        api_base,
        region,
        log=log,
        run_id=run_id,
        test_name=request.node.name,
        cluster_project=project,
        ig_project=peer_project,
    )
    log.done()
