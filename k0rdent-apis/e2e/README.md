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

| Path / object | Purpose |
|---|---|
| `/opt/ufo_simulator/venvs/e2e` | Python venv with pytest + deps |
| `/opt/ufo_simulator/venvs/e2e/env` | Sourceable env (activates venv, sets API/KUBECONFIG/login vars) |
| `kcm-system/host-cluster-a-kubeconfig` Secret | Management-cluster kubeconfig (`value` key) for HCP |
| `kcm-system/hcp-host-clusters` ConfigMap | Maps ClusterType `nico-verity-hcp` → that Secret |

Re-run the playbook after `requirements.txt` changes to refresh packages, or
whenever HCP creates fail with `no host cluster registered for ClusterType …`.

### 2. Source env and run

```bash
source /opt/ufo_simulator/venvs/e2e/env
cd "$E2E_DIR"
```

`source` activates the venv and exports `API_BASE`, `PROJECT`, `KUBECONFIG`,
and login settings. The operator JWT is minted in Python on first API call
(`helpers/auth.py` — same auth + mock-oauth2 flow as bash `k0r_login`).

```bash
# All smoke tests (HCP + BMaaS create + security-group scenarios)
# -s is required to see live STEP progress on stderr
pytest -m smoke -s

# Single files
pytest tests/test_security_groups_effective.py -s   # seconds; creates nothing
pytest tests/test_hcp_cluster.py -s
pytest tests/test_hcp_cluster_security_groups.py -s
pytest tests/test_instance_group.py -s
pytest tests/test_instance_group_security_groups.py -s
pytest tests/test_vpc_peering_intra_project.py -s
pytest tests/test_vpc_peering_inter_project.py -s

# By marker
pytest -m hcp -s
pytest -m bmaas -s

# VPC peering — needs MOCK_MODE off and two free nico-lab servers.
# Both tests carry `peering`; only the same-project one carries `smoke`.
pytest -m "peering and not crossorg" -s   # same-project handshake alone
pytest -m peering -s                      # both, incl. the extra IG in E2E_PEER_PROJECT
pytest -m crossorg -s                     # cross-org only
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
- `prepare-e2e-tests.yml` has registered the HCP host-cluster Secret + ConfigMap
  (required by `ResolveHCPHostCluster` for `nico-verity-hcp`)

Tests **create** missing region-scoped prerequisites (address pools, HCP + BM
cluster types) and leave them in place. They delete the clusters / instance
groups they create.

The `security-groups-effective` assertions need a k0rdent-apis build carrying
**KNF-469**. The lab builds from the `/opt/ufo_lab/k0rdent-apis` checkout on the
CMP (see `ansible/k0rdent-apis.yml`), so that checkout decides whether the
endpoint exists. Each SG scenario probes the route once and skips only its
effective steps when it is absent; `test_security_groups_effective.py` skips
whole.

---

## Environment variables

Set automatically by `source …/env`:

| Variable | Default | Meaning |
|---|---|---|
| `API_BASE` / `BASE` | `http://10.200.0.254:30080` | k0rdent-apis Kong URL |
| `E2E_REGION` / `REGION` | `local` | Region path segment |
| `PROJECT` | `kind-main` | Tenant project id |
| `E2E_PEER_PROJECT` | `acme-main` | Second project, in a **different org**, for the cross-org peering test |
| `KUBECONFIG` | `/root/.kube/config` | Cluster for CR asserts + token mint |
| `K0R_TOKEN_FILE` | `/tmp/k0r-token` | JWT cache (mode 0600) |
| `TOKEN_SLACK_SEC` | `60` | Re-mint if expiring within N seconds |
| `K0R_NAMESPACE` | `k0rdent-apis` | Namespace of `auth` / `mock-oauth2-server` |
| `K0R_LOGIN_EMAIL` | `admin@kind.test` | Login initiate email |
| `K0R_LOGIN_CLIENT_ID` | `operator-portal` | OAuth client id |
| `K0R_LOGIN_REDIRECT_URI` | `$API_BASE/…/auth/callback` | OAuth redirect |
| `E2E_DIR` | `…/k0rdent-apis/e2e` | Suite root |
| `E2E_RUN_ID` | random 8-hex | Short run id stamped into every test-created resource `id` |

Override before `source` or after, e.g. `export PROJECT=my-project`.

Resource ids for clusters, instance groups, and security groups are
`{test-name-without-test_}-{kind}-{run_id}` (≤63 chars), e.g.
`hcp-cluster-vpc-security-groups-cluster-a1b2c3d4`. Shared globals
(address pools, cluster types) keep stable names and are never deleted.

---

## What each test does

| Test | Marker | Summary |
|---|---|---|
| `test_hcp_cluster.py` | `smoke`, `hcp` | Ensure address pools + cluster types; create HCP cluster; wait API `active` + ClusterDeployment Ready; delete |
| `test_hcp_cluster_security_groups.py` | `smoke`, `hcp` | Create cluster; VPC default + custom SG; cluster SG; UFO CRs; NICo NSG merge + precedence; effective read at every binding stage (`objectKind=cluster`: merge order, attribution, agreement with the NICo NSG); detach and assert rules leave both the NSG and the effective block; teardown |
| `test_instance_group.py` | `smoke`, `bmaas` | Ensure address pools + cluster types; create BMaaS instance group; wait API `active`; delete |
| `test_instance_group_security_groups.py` | `smoke`, `bmaas` | Create IG; VPC default + custom SG; IG SG; UFO CRs; NICo NSG merge + precedence; effective read at every binding stage (`objectKind=instance_group`: merge order, attribution, agreement with the NICo NSG); detach and assert rules leave both the NSG and the effective block; teardown |
| `test_security_groups_effective.py` | `smoke` | `security-groups-effective` negatives (422 on missing/unknown `objectKind`/`objectId`, 404 on an unknown or cross-kind id) and the `vpc` arm against a materialized VPC. Creates nothing, needs no cluster — run it first to confirm Kong routes the path |
| `test_vpc_peering_intra_project.py` | `smoke`, `peering` | Cluster + IG in one project (provisioned concurrently); peer their nico VPCs both ways; assert the UFO `VpcPeering` CRs, that one side alone programs nothing, and that the mutual pair collapses onto exactly one NICo `VPCPeering`; teardown |
| `test_vpc_peering_inter_project.py` | `peering`, `crossorg` | Same handshake across two orgs — cluster in `kind-main` (org `kind`) ↔ IG in `acme-main` (org `acme`); additionally asserts `spec.remote.namespace` and that neither side is listed under the other's VPC |

Do not run these against the same project in parallel — they share address
pools / cluster types. Per-run resource ids (test name + `E2E_RUN_ID`) avoid
collisions across sequential runs; use a distinct `PROJECT` for true parallel
suites.

The peering tests have two extra requirements:

- **`MOCK_MODE` must be off** (`lab-inject.sh mock off`). Under mock,
  `VPCPeeringCreate` returns before applying anything, so no UFO `VpcPeering`
  CR exists and every CR assertion fails. Closing exactly that gap is the point
  of these tests.
- **Two available `nico-lab` servers.** A cluster and an instance group are
  alive simultaneously (they are POSTed together so provisioning overlaps, then
  awaited together). Every other scenario needs only one.

`crossorg` is a *narrowing* marker, not an exclusion: both peering tests carry
`peering`, so `-m peering` runs both. Use `-m "peering and not crossorg"` to skip
the cross-org one. Only the same-project test carries `smoke`, so the default
`-m smoke` run never provisions in the peer project.

---

## Layout

```
e2e/
  conftest.py          # fixtures; RefreshingBearerAuth
  pytest.ini
  requirements.txt
  helpers/
    api.py             # REST create/get/delete / VPC+cluster/IG SG bind
    auth.py            # Python k0r_login/k0r_token (mint + cache)
    names.py           # per-run resource ids ({test}-{kind}-{run_id})
    k8s.py             # list/get CRs; find UFO/NICo peering objects by label + owner
    secgroups.py       # SG lifecycle, one rule vocabulary, effective-read asserts
    vpc_peering.py     # shared peering handshake (provision, CR/backend asserts)
    steps.py           # numbered runtime STEP progress (pytest -s)
    wait.py            # await_predicate / await_api_state / await_api_states
  tests/
    test_hcp_cluster.py
    test_hcp_cluster_security_groups.py
    test_instance_group.py
    test_instance_group_security_groups.py
    test_security_groups_effective.py
    test_vpc_peering_intra_project.py
    test_vpc_peering_inter_project.py
```

`helpers/secgroups.py` holds everything both SG scenarios share. Its
`rule_fingerprint` is the one vocabulary the four surfaces are compared in — the
API security group, the UFO `SecurityGroup` CR, the NICo `NetworkSecurityGroup`
CR, and `security-groups-effective` each spell a rule differently, so a second
copy that drifted would silently compare nothing.

`helpers/vpc_peering.py` holds the handshake shared by the intra- and
inter-project peering tests — provision cluster + IG, declare both halves,
assert UFO/NICo objects, tear down.

Templates live under [`../scenarios/templates`](../scenarios/templates):

```
scenarios/templates/
  global/                              # shared; ensure, never tear down
    address-pool-global-*.yaml
    cluster-type-nico-verity-hcp.yaml
    cluster-type-nico-verity-bm.yaml
  hcp_cluster/                         # test_hcp_cluster.py + vpc peering tests
    cluster.yaml
  hcp_cluster_security_groups/         # test_hcp_cluster_security_groups.py
    cluster.yaml
    security-group-*.yaml
    vpc-security-groups.yaml
  instance_group/                      # test_instance_group.py + vpc peering tests
    instance-group.yaml
  instance_group_security_groups/      # test_instance_group_security_groups.py
    instance-group.yaml
    security-group-*.yaml
  vpc_peering/                         # test_vpc_peering_{intra,inter}_project.py
    peering.yaml
```

The peering tests deliberately reuse the `hcp_cluster` and `instance_group`
bodies rather than keeping their own copies, so those two are no longer
single-consumer — edit them with that in mind.

---

## Troubleshooting

| Symptom | Check |
|---|---|
| `k0r_login: …` / mint failure | `kubectl -n k0rdent-apis get svc auth mock-oauth2-server`; `KUBECONFIG` reaches the CMP cluster |
| Tests skipped (`API_BASE required`) | `source` the env file; confirm `echo $API_BASE` |
| `401` mid-run | Should auto-remint once; if it persists, check kube access for `k0r_login` |
| Cluster stuck `creating` / timeout | Lab capacity, NICo inventory, UFO/NetworkBundle events in `prj-$PROJECT` |
| `no host cluster registered for ClusterType "nico-verity-hcp"` | Re-run `ansible-playbook prepare-e2e-tests.yml`; check `kubectl -n kcm-system get cm hcp-host-clusters -o yaml` |
| SG attach `409 CONFLICT_IN_USE` | Wait for VPC/cluster `active` before the next binding write (tests already poll) |
| Peering DELETE `409 CONFLICT_IN_USE` | DELETE is a CAS over `state IN ('active','failed')`; against a row still `creating` it refuses and the row keeps its direction. Settle first (the tests already do) |
| Peering create `409` "already peered" | The previous run's row is not tombstoned yet — `uq_vpc_peering_direction` is partial on `deleted_at IS NULL`, so a direction frees only at tombstone, not at the 204 |
| UFO `VpcPeering` CR never appears | `MOCK_MODE` is on — the workflow returns before `ApplyUFOVpcPeering`. `lab-inject.sh mock off` |
| Backend `VPCPeering` never appears | Only one side is declared. A one-sided peering programs no fabric by design; both mutual CRs must exist |

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
