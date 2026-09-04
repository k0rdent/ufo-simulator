# Lab e2e for k0rdent-apis

Scenario tests against a live UFO lab (CMP + k0rdent-apis + Kubernetes). They
POST YAML from [`../scenarios/templates`](../scenarios/templates), wait for API
`state`, and assert materialized CRs in `prj-<project>`.

This is **not** the upstream `MOCK_MODE` harness in the k0rdent-apis repo.

---

## How to run

All steps are on the **CMP** (needs `kubectl` to the lab cluster, Kong at
`API_BASE`, and NICo/UFO already installed).

### 1. One-time prepare (venv + env file)

```bash
cd /opt/ufo_lab/ufo-simulator/ansible
ansible-playbook prepare-e2e-tests.yml
```

This creates:

| Path | Purpose |
|---|---|
| `/opt/ufo_simulator/venvs/e2e` | Python venv with pytest + deps |
| `/opt/ufo_simulator/venvs/e2e/env` | Sourceable env (activates venv, sets API/KUBECONFIG/login vars) |

Re-run the playbook after `requirements.txt` changes to refresh packages.

### 2. Source env and run

```bash
source /opt/ufo_simulator/venvs/e2e/env
cd "$E2E_DIR"
```

`source` activates the venv and exports `API_BASE`, `PROJECT`, `KUBECONFIG`,
and login settings. The operator JWT is minted in Python on first API call
(`helpers/auth.py` — same auth + mock-oauth2 flow as bash `k0r_login`).

```bash
# All smoke tests (HCP create + security-group scenario)
# -s is required to see live STEP progress on stderr
pytest -m smoke -s

# Single files
pytest tests/test_hcp_cluster.py -s
pytest tests/test_hcp_cluster_security_groups.py -s

# By marker
pytest -m hcp -s
```

`-s` shows print/log output; default timeout is 1800s (`pytest.ini`).

### 3. Token refresh

Every API call goes through `AuthedSession`:

1. **Proactive** — `get_token()` before the request; remints if the cached JWT
   is within `TOKEN_SLACK_SEC` (60s) of expiry (same rule as bash `k0r_token`).
2. **On demand** — if the API returns `401`, invalidate the cache, mint a new
   JWT, and retry the same request once.

Cache file: `/tmp/k0r-token` (`K0R_TOKEN_FILE`). Force a remint:

```bash
rm -f /tmp/k0r-token
```

---

## Prerequisites

- k0rdent-apis playbook has been applied (`ansible-playbook k0rdent-apis.yml`)
- Project namespace exists (default `prj-kind-main`)
- CMP can reach Kong (`http://10.200.0.254:30080`) and in-cluster
  `auth` / `mock-oauth2-server` Services (for token minting)

Tests **create** missing region-scoped prerequisites (address pools, cluster
type) and leave them in place. They delete the clusters they create.

---

## Environment variables

Set automatically by `source …/env`:

| Variable | Default | Meaning |
|---|---|---|
| `API_BASE` / `BASE` | `http://10.200.0.254:30080` | k0rdent-apis Kong URL |
| `E2E_REGION` / `REGION` | `local` | Region path segment |
| `PROJECT` | `kind-main` | Tenant project id |
| `KUBECONFIG` | `/root/.kube/config` | Cluster for CR asserts + token mint |
| `K0R_TOKEN_FILE` | `/tmp/k0r-token` | JWT cache (mode 0600) |
| `TOKEN_SLACK_SEC` | `60` | Re-mint if expiring within N seconds |
| `K0R_NAMESPACE` | `k0rdent-apis` | Namespace of `auth` / `mock-oauth2-server` |
| `K0R_LOGIN_EMAIL` | `admin@kind.test` | Login initiate email |
| `K0R_LOGIN_CLIENT_ID` | `operator-portal` | OAuth client id |
| `K0R_LOGIN_REDIRECT_URI` | `$API_BASE/…/auth/callback` | OAuth redirect |
| `E2E_DIR` | `…/k0rdent-apis/e2e` | Suite root |

Override before `source` or after, e.g. `export PROJECT=my-project`.

---

## What each test does

| Test | Marker | Summary |
|---|---|---|
| `test_hcp_cluster.py` | `smoke`, `hcp` | Ensure address pools + cluster type; create HCP cluster; wait API `active` + ClusterDeployment Ready; delete |
| `test_hcp_cluster_security_groups.py` | `smoke`, `hcp` | Create cluster; VPC default + custom SG; cluster SG; UFO CRs; NICo NSG merge + precedence; detach and assert rules leave NICo NSG; teardown |

Do not run both against the same project in parallel — they use different
cluster ids (`lab-nico-verity-21` vs `lab-nico-verity-sg`) but share address
pools / cluster type / SG templates.

---

## Layout

```
e2e/
  conftest.py          # fixtures; RefreshingBearerAuth
  pytest.ini
  requirements.txt
  helpers/
    api.py             # REST create/get/delete / VPC+cluster SG bind
    auth.py            # Python k0r_login/k0r_token (mint + cache)
    k8s.py             # list/get CRs in project namespace
    steps.py           # numbered runtime STEP progress (pytest -s)
    wait.py            # await_predicate / await_api_state
  tests/
    test_hcp_cluster.py
    test_hcp_cluster_security_groups.py
```

Templates live under [`../scenarios/templates`](../scenarios/templates):

```
scenarios/templates/
  global/                              # shared; ensure, never tear down
    address-pool-global-*.yaml
    cluster-type-nico-verity-hcp.yaml
    cluster-type-nico-verity-bm.yaml
  hcp_cluster/                         # test_hcp_cluster.py
    cluster.yaml
  hcp_cluster_security_groups/         # test_hcp_cluster_security_groups.py
    cluster.yaml
    security-group-*.yaml
    vpc-security-groups.yaml
  instance_group/                      # BMaaS (manual / future e2e)
    instance-group.yaml
```

---

## Troubleshooting

| Symptom | Check |
|---|---|
| `k0r_login: …` / mint failure | `kubectl -n k0rdent-apis get svc auth mock-oauth2-server`; `KUBECONFIG` reaches the CMP cluster |
| Tests skipped (`API_BASE required`) | `source` the env file; confirm `echo $API_BASE` |
| `401` mid-run | Should auto-remint once; if it persists, check kube access for `k0r_login` |
| Cluster stuck `creating` / timeout | Lab capacity, NICo inventory, UFO/NetworkBundle events in `prj-$PROJECT` |
| SG attach `409 CONFLICT_IN_USE` | Wait for VPC/cluster `active` before the next binding write (tests already poll) |

Optional wipe of leftovers in the project namespace:

```bash
../scripts/force-wipe-ns.sh prj-kind-main
```

---

## Design notes

| Piece | Choice |
|---|---|
| Runner | pytest + pytest-timeout |
| HTTP | requests + PyYAML |
| Auth | Python `helpers/auth.py` (port of k0r_login/k0r_token) |
| K8s | kubernetes Python client |

Not used here: Chainsaw/KUTTL (weak REST create path), bats, upstream Go Ginkgo.
