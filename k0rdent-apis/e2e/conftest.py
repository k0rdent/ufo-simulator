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
    ("global/cluster-type-nico-hcp.yaml", "compute/cluster-types"),
    ("global/cluster-type-netris-hcp-ew.yaml", "compute/cluster-types"),
)

# Verity-only cluster types, created by ensure_verity_prereqs rather than being
# global: nothing on a netris lab can deploy from them.
VERITY_PREREQS: tuple[tuple[str, str], ...] = (
    ("global/cluster-type-nico-verity-hcp-ew.yaml", "compute/cluster-types"),
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
def peer_project() -> str:
    """Second project, in a DIFFERENT org, for cross-org peering.

    `acme-main` (org `acme`) is declared by the k0rdent-apis provision manifest
    alongside `kind-main` (org `kind`), so no extra provisioning is needed. The
    e2e identity can write to it because the lab's
    02-manifest-admin-roles.patch binds admin@kind.test compute-admin at
    PLATFORM scope, not just on kind-main.
    """
    return os.environ.get("E2E_PEER_PROJECT", "acme-main")


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


def _ensure_prereqs(session, api_base: str, region: str, prereqs) -> None:
    """Create each (template, resource) if missing; never delete.

    Anything that reports ``state`` must be ``active`` before an owner can bind
    it — creating one while an address pool is still ``creating`` is a 409 — so
    wait here and callers can POST immediately after this returns.

    Note ``api.ensure_exists`` only creates. Once a cluster type exists in the
    lab, editing its template changes nothing until it is deleted by hand.
    """
    from helpers import api, wait

    for template_name, resource in prereqs:
        body = load_template(template_name)
        collection = api.region_url(api_base, region, resource)
        obj = api.ensure_exists(session, collection, body)
        item_url = f"{collection}/{body['id']}"
        # Cluster types have no async lifecycle today; only wait when the
        # resource actually reports a state machine.
        if obj.get("state") is None:
            continue
        wait.await_api_state(
            lambda url=item_url: api.get_json(session, url),
            "active",
            what=f"{resource} {body['id']}",
            timeout=900,
            interval=5,
        )


def ensure_global_prereqs(session, api_base: str, region: str) -> None:
    """Create the shared address pools + cluster types every lab needs."""
    _ensure_prereqs(session, api_base, region, GLOBAL_PREREQS)


def ensure_verity_prereqs(session, api_base: str, region: str) -> None:
    """Create the Verity cluster types. Only the verity-gated tests call this.

    Kept out of GLOBAL_PREREQS so a netris lab's runs are untouched: a verity
    cluster type there would be an object nothing can ever deploy from.
    """
    _ensure_prereqs(session, api_base, region, VERITY_PREREQS)


def fabric_backend() -> str:
    """Which fabric this lab runs, for the per-backend east-west skipifs.

    Set by ansible/templates/e2e-env.sh.j2 from the lab's own sdn_provider, so
    `source .../env` gates correctly with no flags. Empty when unset, which
    skips every backend-specific test rather than guessing one.
    """
    return (os.environ.get("E2E_FABRIC_BACKEND") or "").strip().lower()


def verity_backend_name() -> str:
    """The UFO backend *instance* carrying the Spectrum-X config.

    A key under ``backends.fabric`` in ufo_conf.yaml, not a driver name — two
    verity instances exist and only the one with site + fabric_type takes the
    Spectrum-X path.
    """
    return (os.environ.get("E2E_VERITY_BACKEND") or "verity-ewf").strip()


# Re-export for skipif markers in tests.
auth_configured = _auth_configured
