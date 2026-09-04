"""Unique resource ids for a lab e2e run: {test}-{kind}-{run_sha}."""

from __future__ import annotations

import os
import re
import secrets
from typing import Any

# K0rdent resource id: 1–63 chars, [a-z]([-a-z0-9]*[a-z0-9])?
_ID_RE = re.compile(r"^[a-z]([-a-z0-9]*[a-z0-9])?$")
_MAX_LEN = 63


def new_run_id() -> str:
    """Short hex for this pytest session (override with E2E_RUN_ID)."""
    env = (os.environ.get("E2E_RUN_ID") or "").strip().lower()
    if env:
        if not re.fullmatch(r"[a-z0-9]{4,16}", env):
            raise ValueError(
                f"E2E_RUN_ID must be 4–16 [a-z0-9], got {env!r}"
            )
        return env
    return secrets.token_hex(4)  # 8 hex chars


def resource_id(test_name: str, kind: str, *, run_id: str) -> str:
    """
    Build a unique slug id from the pytest node name, a short role, and run_id.

    Example: test_hcp_cluster_vpc_security_groups + demo-sg + a1b2c3d4
      → hcp-cluster-vpc-security-groups-demo-sg-a1b2c3d4
    """
    test = test_name.removeprefix("test_").replace("_", "-").lower()
    test = re.sub(r"[^a-z0-9-]+", "-", test).strip("-")
    kind = kind.replace("_", "-").lower()
    kind = re.sub(r"[^a-z0-9-]+", "-", kind).strip("-")
    run = run_id.lower()

    if not test or not kind or not run:
        raise ValueError(f"empty component: test={test!r} kind={kind!r} run={run!r}")

    suffix = f"-{kind}-{run}"
    budget = _MAX_LEN - len(suffix)
    if budget < 1:
        raise ValueError(f"kind+run_id too long for {_MAX_LEN}-char id: {suffix!r}")
    prefix = test[:budget].rstrip("-")
    if not prefix or not prefix[0].isalpha():
        prefix = f"t{prefix}"[:budget].rstrip("-")

    out = f"{prefix}{suffix}"
    if not _ID_RE.fullmatch(out):
        raise ValueError(f"generated id fails API pattern: {out!r}")
    return out


def stamp_id(body: dict[str, Any], new_id: str) -> dict[str, Any]:
    """Return a shallow copy of a template body with id overwritten."""
    out = dict(body)
    out["id"] = new_id
    return out
