"""Thin REST helpers: create/get/delete + URL shapes matching k0r.sh."""

from __future__ import annotations

from typing import Any

import requests


def region_url(api_base: str, region: str, resource: str, project: str | None = None) -> str:
    """Build /v1/regions/{region}/[projects/{project}/]{resource}."""
    base = f"{api_base}/v1/regions/{region}"
    if project:
        return f"{base}/projects/{project}/{resource}"
    return f"{base}/{resource}"


def create(
    session: requests.Session,
    url: str,
    body: dict[str, Any],
) -> requests.Response:
    r = session.post(url, json=body, timeout=60)
    r.raise_for_status()
    return r


def get(session: requests.Session, url: str) -> requests.Response:
    return session.get(url, timeout=30)


def delete(session: requests.Session, url: str) -> requests.Response:
    return session.delete(url, timeout=60)


def ensure_exists(
    session: requests.Session,
    collection_url: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    """GET by body id; create if missing. Leaves the resource in place."""
    resource_id = body["id"]
    item_url = f"{collection_url}/{resource_id}"
    existing = get(session, item_url)
    if existing.status_code == 200:
        return existing.json()
    if existing.status_code != 404:
        existing.raise_for_status()

    created = session.post(collection_url, json=body, timeout=60)
    # Concurrent create from another runner is fine.
    if created.status_code in (200, 201):
        return created.json()
    if created.status_code == 409:
        again = get(session, item_url)
        again.raise_for_status()
        return again.json()
    created.raise_for_status()
    return created.json()


def list_items(
    session: requests.Session,
    collection_url: str,
    *,
    params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """GET a collection; unwrap common list envelopes."""
    resp = session.get(collection_url, params=params, timeout=30)
    resp.raise_for_status()
    body = resp.json()
    if isinstance(body, list):
        return body
    for key in ("items", "vpcs", "securityGroups", "clusters", "instanceGroups"):
        if key in body and isinstance(body[key], list):
            return body[key]
    raise AssertionError(f"unexpected list envelope from {collection_url}: {body!r}")


def get_json(session: requests.Session, url: str) -> dict[str, Any]:
    resp = get(session, url)
    resp.raise_for_status()
    return resp.json()


def set_vpc_security_groups(
    session: requests.Session,
    vpc_url: str,
    security_group_ids: list[str],
) -> dict[str, Any]:
    """POST .../networking/vpcs/{id} — replace binding (not merge)."""
    resp = session.post(
        vpc_url,
        json={"securityGroups": security_group_ids},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def set_cluster_security_groups(
    session: requests.Session,
    cluster_url: str,
    security_group_ids: list[str],
) -> dict[str, Any]:
    """PATCH .../compute/clusters/{id} with only securityGroups (async 202)."""
    resp = session.patch(
        cluster_url,
        json={"securityGroups": security_group_ids},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def set_instance_group_security_groups(
    session: requests.Session,
    instance_group_url: str,
    security_group_ids: list[str],
) -> dict[str, Any]:
    """PATCH .../compute/instance-groups/{id} with only securityGroups (async 202)."""
    resp = session.patch(
        instance_group_url,
        json={"securityGroups": security_group_ids},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()
