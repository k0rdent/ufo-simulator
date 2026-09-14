# Global (shared) templates

Region-scoped resources that **every** lab e2e scenario needs and that
tests must **not** delete on teardown.

| File | API |
|---|---|
| `address-pool-global-default.yaml` | `compute/address-pools` |
| `address-pool-global-public.yaml` | `compute/address-pools` |
| `cluster-type-nico-hcp.yaml` | `compute/cluster-types` (HCP; also used by instance-group scenarios) |
| `cluster-type-netris-hcp-ew.yaml` | `compute/cluster-types` (HCP + Netris E/W rails eth-ew0..3; fixed `cidr: 10.0.0.0/8`) |

Create with `ensure_exists` / `ensure_global_prereqs` (or `k0r.sh create`);
leave in place after the run.

Scenario-specific bodies live in sibling folders named after the e2e scenario
(`hcp_cluster/`, `hcp_cluster_ew/`, `hcp_cluster_security_groups/`, …).
