"""Shared fixtures for lab e2e against k0rdent-apis + Kubernetes."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import requests
import yaml

from helpers.auth import AuthedSession, get_token
from helpers.names import new_run_id

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "scenarios" / "templates"

# Shared region-scoped resources: ensure before each test, never tear down.
GLOBAL_PREREQS: tuple[tuple[str, str], ...] = (
    ("global/address-pool-global-default.yaml", "compute/address-pools"),
    ("global/address-pool-global-public.yaml", "compute/address-pools"),
    ("global/cluster-type-nico-verity-hcp.yaml", "compute/cluster-types"),
    ("global/cluster-type-nico-verity-bm.yaml", "compute/cluster-types"),
)


def _auth_configured() -> bool:
    """True when API_BASE is set; JWT is minted in-process via helpers.auth."""
    return bool(os.environ.get("API_BASE"))


@pytest.fixture(scope="session")
def run_id() -> str:
    """Short hex shared by all tests in this pytest process (see E2E_RUN_ID)."""
    return new_run_id()


@pytest.fixture(scope="session")
def api_base() -> str:
    return os.environ["API_BASE"].rstrip("/")


@pytest.fixture(scope="session")
def token() -> str:
    """Initial token; prefer get_token() / RefreshingBearerAuth for live calls."""
    return get_token()


@pytest.fixture(scope="session")
def project() -> str:
    return os.environ.get("PROJECT", "kind-main")


@pytest.fixture(scope="session")
def region() -> str:
    return os.environ.get("E2E_REGION") or os.environ.get("REGION") or "local"


@pytest.fixture(scope="session")
def session() -> requests.Session:
    """HTTP session: fresh JWT per request; remint + retry once on 401."""
    s = AuthedSession()
    s.headers.update(
        {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
    )
    return s


@pytest.fixture(scope="session")
def templates_dir() -> Path:
    return TEMPLATES


def load_template(name: str) -> dict:
    """Load YAML from scenarios/templates (supports subpaths, e.g. global/…)."""
    path = TEMPLATES / name
    with path.open() as f:
        return yaml.safe_load(f)


def load_scenario_template(scenario: str, name: str) -> dict:
    """Load a template from scenarios/templates/<scenario>/<name>."""
    return load_template(f"{scenario}/{name}")


def ensure_global_prereqs(session, api_base: str, region: str) -> None:
    """Create shared address pools + cluster types if missing; never delete them."""
    from helpers import api

    for template_name, resource in GLOBAL_PREREQS:
        body = load_template(template_name)
        collection = api.region_url(api_base, region, resource)
        api.ensure_exists(session, collection, body)


# Re-export for skipif markers in tests.
auth_configured = _auth_configured
