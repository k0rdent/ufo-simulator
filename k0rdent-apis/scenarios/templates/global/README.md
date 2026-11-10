# Global (shared) templates

Region-scoped resources that **every** lab e2e scenario needs and that
tests must **not** delete on teardown.

ClusterTypes take their networking from **network schema templates** and the
**network attachment templates** nested under them (k0rdent-apis KNF-460):
`networkSchemaRef.template` on the type, `networkAttachmentRef.template` on each
node pool. Neither carries `networkSchema` / `networkAttachment` inline.

| File | API |
|---|---|
| `address-pool-global-default.yaml` | `compute/address-pools` |
| `address-pool-global-public.yaml` | `compute/address-pools` |
| `network-schema-template-nico.yaml` | `networking/network-schema-templates` (single NICo VPC) |
| `network-schema-template-nico-netris-ew.yaml` | `networking/network-schema-templates` (NICo + Netris E/W + NVLink) |
| `network-schema-template-nico-verity-ew.yaml` | `networking/network-schema-templates` (NICo + Verity Spectrum-X E/W) |
| `network-attachment-template-nico-lab.yaml` | `networking/network-schema-templates/nico/network-attachment-templates` |
| `network-attachment-template-nico-lab-netris-ew.yaml` | `networking/network-schema-templates/nico-netris-ew/network-attachment-templates` |
| `network-attachment-template-nico-lab-verity-ew.yaml` | `networking/network-schema-templates/nico-verity-ew/network-attachment-templates` |
| `cluster-type-nico-hcp.yaml` | `compute/cluster-types` (HCP; also used by instance-group scenarios) → `nico` / `nico-lab` |
| `cluster-type-netris-hcp-ew.yaml` | `compute/cluster-types` (HCP + Netris E/W rails eth-ew0..3; also used by `instance_group_ew`) → `nico-netris-ew` / `nico-lab-netris-ew` |
| `cluster-type-nico-verity-hcp-ew.yaml` | `compute/cluster-types` (HCP + Verity E/W; verity labs only) → `nico-verity-ew` / `nico-lab-verity-ew` |
| `cluster-type-nico-verity-ew.yaml` | `compute/cluster-types` (BMaaS + Verity E/W; no test deploys it) → same templates as above |

Create in dependency order — address pools, schema templates, attachment
templates, cluster types — with `ensure_global_prereqs` /
`ensure_verity_prereqs` (or `k0r.sh create`); leave in place after the run.

Network templates cannot change after create (only their `status` moves,
forward), and cannot be deleted while a ClusterType references them. To change
one, delete in reverse order — the ClusterTypes that reference it, then the
attachment template, then the schema template — and let the next run recreate
them. `ensure_exists` only creates, so an edited YAML never reaches a lab that
already holds the object.

Scenario-specific bodies live in sibling folders named after the e2e scenario
(`hcp_cluster/`, `hcp_cluster_ew/`, `hcp_cluster_security_groups/`, …).
