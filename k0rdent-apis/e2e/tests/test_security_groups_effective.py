"""Negative paths and the vpc arm of ``security-groups-effective`` (KNF-469).

These create nothing and need no cluster, so they live outside the two long SG
scenarios rather than behind a 30-minute build. Running this file first also says
whether Kong routes the new path in this lab at all — the merge coverage in
``test_hcp_cluster_security_groups.py`` / ``test_instance_group_security_groups.py``
skips itself when it does not.

The expected status codes are deliberately the same ones the upstream MOCK_MODE
suite pins (``k0rdent-apis/tests/e2e/compute/tests/test_security_groups_effective.py``),
so the two suites cannot drift.
"""

from __future__ import annotations

from typing import Any

import pytest

from conftest import auth_configured
from helpers import api, secgroups

pytestmark = [pytest.mark.smoke]


@pytest.fixture(scope="module")
def effective(session, api_base, region, project):
    """Skip the whole module when this lab predates KNF-469."""
    if not secgroups.effective_endpoint_available(session, api_base, region, project):
        pytest.skip(
            "security-groups-effective is not registered; the k0rdent-apis "
            "checkout on this CMP predates KNF-469"
        )

    def _raw(**params: Any):
        return secgroups.get_effective_raw(
            session, api_base, region, project, params=params
        )

    return _raw


@pytest.fixture(scope="module")
def nico_vpc(session, api_base, region, project) -> dict[str, Any]:
    """Any nico-backed VPC in the project, or skip.

    The suite cannot create one — VPCs are materialized by the cluster /
    instance-group workflows — so this is conditional the way the upstream tests
    are.
    """
    vpcs_url = api.region_url(api_base, region, "networking/vpcs", project=project)
    for vpc in api.list_items(session, vpcs_url):
        if (vpc.get("backend") or "").lower() == secgroups.NICO_BACKEND:
            return vpc
    pytest.skip("no materialized nico-backed VPC in this project")


@pytest.mark.skipif(not auth_configured(), reason="API_BASE required")
@pytest.mark.parametrize(
    "params",
    [
        pytest.param({"objectId": "e2e-stub-cluster"}, id="missing-objectKind"),
        pytest.param({"objectKind": "cluster"}, id="missing-objectId"),
        pytest.param({}, id="missing-both"),
    ],
)
def test_effective_missing_params_is_422(effective, params):
    """Both parameters are required. Omitting one is a 422 rather than a
    default-to-everything listing: the answer is only meaningful per object."""
    resp = effective(**params)
    assert resp.status_code == 422, (
        f"params={params!r} must be 422, got {resp.status_code}: {resp.text[:300]}"
    )


@pytest.mark.skipif(not auth_configured(), reason="API_BASE required")
def test_effective_unknown_object_kind_is_422(effective):
    """An unrecognized kind is 422, not 404: a typo must not be reported as a
    missing resource, which would send the caller looking for the wrong problem.

    ``nodepool`` specifically — a node pool's id is unique only within its parent,
    so it cannot be addressed by ``objectId`` alone and is rejected by design.
    """
    resp = effective(objectKind="nodepool", objectId="workers")
    assert resp.status_code == 422, (
        f"unknown objectKind must be 422, got {resp.status_code}: {resp.text[:300]}"
    )


@pytest.mark.skipif(not auth_configured(), reason="API_BASE required")
def test_effective_unknown_object_is_404(effective):
    resp = effective(objectKind="cluster", objectId="e2e-no-such-cluster")
    assert resp.status_code == 404, (
        f"unknown objectId must be 404, got {resp.status_code}: {resp.text[:300]}"
    )


@pytest.mark.skipif(not auth_configured(), reason="API_BASE required")
def test_effective_rejects_a_cross_kind_id(effective, nico_vpc):
    """A VPC id under objectKind=cluster is 404, not a coincidental hit: the lookup
    is per kind, so ids never resolve across resource types.

    404 exactly, not "some 4xx": ``cluster`` is a valid kind and a VPC id satisfies
    the objectId pattern, so the request reaches the cluster lookup and misses.
    Accepting 422 here would let a regression that rejects the id before looking it
    up pass as the documented behaviour.
    """
    resp = effective(objectKind="cluster", objectId=nico_vpc["id"])
    assert resp.status_code == 404, (
        f"a VPC id under objectKind=cluster must be 404, got {resp.status_code}: "
        f"{resp.text[:300]}"
    )


@pytest.mark.skipif(not auth_configured(), reason="API_BASE required")
def test_effective_vpc_read_agrees_with_the_vpc_resource(
    effective, nico_vpc, session, api_base, region, project
):
    """A vpc read reports that VPC's OWN binding — the same ids ``GET /vpcs/{id}``
    lists, in the same order, expanded with attribution and rules.

    Pinned because the arm is easy to "improve" into merging the owner's groups in,
    which would silently change what the operation answers.
    """
    vpc_id = nico_vpc["id"]
    vpc_url = api.region_url(
        api_base, region, f"networking/vpcs/{vpc_id}", project=project
    )
    vpc = api.get_json(session, vpc_url)

    resp = effective(objectKind="vpc", objectId=vpc_id)
    resp.raise_for_status()
    body = resp.json()

    secgroups.assert_effective_object(
        body, kind="vpc", obj_id=vpc_id, uid=vpc["uid"]
    )
    assert [b.get("id") for b in body.get("vpcs") or []] == [vpc_id], (
        "a vpc read returns exactly one block, that VPC: "
        f"{[b.get('id') for b in body.get('vpcs') or []]!r}"
    )

    block = secgroups.effective_block(body, vpc_id)
    assert secgroups.effective_group_ids(block) == list(
        vpc.get("securityGroups") or []
    ), "a vpc read must report exactly the VPC's own binding, in the same order"
    for group in block.get("securityGroups") or []:
        assert group.get("uid"), f"group {group.get('id')!r} came back without a uid"
        assert group.get("source") == "vpc" and group.get("sourceId") == vpc_id, (
            f"group {group.get('id')!r} attributed to {group.get('source')!r}/"
            f"{group.get('sourceId')!r}, want vpc/{vpc_id}"
        )
    secgroups.assert_effective_self_consistent(block)
