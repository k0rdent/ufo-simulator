# Global (shared) templates

Region-scoped resources that **every** lab e2e scenario needs and that
tests must **not** delete on teardown.

| File | API |
|---|---|
| `address-pool-global-default.yaml` | `compute/address-pools` |
| `address-pool-global-public.yaml` | `compute/address-pools` |
| `cluster-type-nico-verity-hcp.yaml` | `compute/cluster-types` (HCP) |
| `cluster-type-nico-verity-bm.yaml` | `compute/cluster-types` (BMaaS; manual / future e2e) |

Create with `ensure_exists` / `ensure_global_prereqs` (or `k0r.sh create`);
leave in place after the run.

Scenario-specific bodies live in sibling folders named after the e2e scenario
(`hcp_cluster/`, `hcp_cluster_security_groups/`, …).
