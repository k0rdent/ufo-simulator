"""VPC peering within one project: cluster-VPC <-> instance-group-VPC.

MOCK BOUNDARY. Requires MOCK_MODE **off** (``lab-inject.sh mock off``). Under
mock, VPCPeeringCreate returns before applying a UFO VpcPeering CR, so every CR
assertion here fails. See ``helpers.vpc_peering`` for the shared handshake and
lab prerequisites (two available ``nico-lab`` servers).
"""

from __future__ import annotations

import pytest

from conftest import auth_configured
from helpers.steps import Steps
from helpers.vpc_peering import peer_cluster_with_instance_group

pytestmark = [pytest.mark.peering]


@pytest.mark.smoke
@pytest.mark.skipif(not auth_configured(), reason="API_BASE required")
def test_vpc_peering_cluster_to_instance_group(
    session, api_base, region, project, run_id, request
):
    """Peer a cluster-owned VPC with an instance-group-owned VPC, same project.

    The cross-ownerKind pairing is permitted by every layer but exercised
    nowhere else: upstream peers cluster<->cluster, the compute-infrastructure
    suite peers instance-group<->instance-group.
    """
    log = Steps("VPC peering: cluster <-> instance group (intra-project)")
    log.info(f"run_id={run_id} project={project}")

    peer_cluster_with_instance_group(
        session,
        api_base,
        region,
        log=log,
        run_id=run_id,
        test_name=request.node.name,
        cluster_project=project,
        ig_project=project,
    )
    log.done()
