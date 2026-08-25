# k0rdent-apis playbook

Automates the k0rdent-apis install steps that ufo-simulator's `deploy/install.sh`
runs when `K0RDENT_APIS_ENABLE=true`. The playbook is also runnable directly
against an already-provisioned CMP node — useful for iteration after a fresh
build.

## What it does

1. Clones the `k0rdent-apis` git repo into `/opt/ufo_lab/k0rdent-apis`.
2. Applies the vendored patches from
   [files/k0rdent-apis/patches/](files/k0rdent-apis/patches/) (idempotently —
   already-applied patches are skipped).
3. Creates the `k0rdent-apis` namespace and applies the `mailpit` manifest.
4. Creates the image-pull `Secret` (from
   `k0rdent_apis_pull_secret_{username,password}` in
   [vars/common.yml](vars/common.yml)), runs `make helm-dep-build`, and creates
   the `nico-service-account` Secret.
5. Renders + applies the NICO platform-admin CRs (`SSHKeyGroup`,
   `NetworkSecurityGroup`) into the `platform-admin` namespace, waits for
   nico-operator to reconcile them (`status.id` populated).
6. Fetches the NICO REST IDs (tenant / ssh-key-group / NSG) via the
   [`nico_fetch_resources`](library/nico_fetch_resources.py) module.
7. Renders
   [values-socks-overrides.yaml.j2](templates/k0rdent-apis/values-socks-overrides.yaml.j2)
   with those IDs, then `helm upgrade --install`s `k0rdent-apis` and
   `k0rdent-kind-extras`.
8. Creates the `provision-manifest` ConfigMap + Secret and applies the
   `provision-apply` Job, waits for it to succeed.
9. Stages the vendored [nico-sync.sh](files/k0rdent-apis/nico-sync.sh) and
   runs it to sync NICO servers into k0rdent-apis. Fails the play if any
   server does not reach `state=available`.
10. Registers the KCM `HelmRepository` + `ClusterTemplate` +
    `ClusterTemplateChain` + `AccessManagement` for `cluster-nico`, so that
    k0rdent-apis `ClusterDeployment` requests can pick up the NICo cluster
    template. Chart name / version live in the `k0rdent_apis_cluster_chart`
    and `k0rdent_apis_cluster_chart_version` vars; the ClusterTemplate and
    Chain names are derived from those (`<chart>-<version-with-dots-as-dashes>`).

## Running it directly against an existing CMP

The k0rdent-apis clone is over HTTPS anonymous, but the checkout the k0rdent-apis
`Makefile` performs during `make helm-dep-build` (and the internal `git`
metadata operations the playbook triggers) may need your git identity /
authenticated remote if you have local uncommitted changes to reset. In
practice, forwarding your local SSH agent to the CMP is enough to cover any
`git@github.com:...` fallbacks:

```bash
# 1. SSH into the CMP with agent forwarding.
ssh -A ubuntu@<cmp-ip>

# 2. Set the k0rdent-apis pull-secret creds (they are placeholders in
#    vars/common.yml until install.sh's sed replaces them at boot, so for a
#    manual re-run edit the file OR export env vars and re-run install.sh's
#    sed lines).
sudo vim /opt/ufo_lab/ufo-simulator/ansible/vars/common.yml
# set:
#   k0rdent_apis_pull_secret_username: <your-username>
#   k0rdent_apis_pull_secret_password: <your-token>

# 3. Run the playbook. -E forwards the SSH_AUTH_SOCK so agent forwarding still
#    works under sudo; --limit constrains the run to the CMP node.
cd /opt/ufo_lab/ufo-simulator/ansible
sudo -E ansible-playbook -i inventory.yml k0rdent-apis.yml --limit cmp01
```

`sudo -E` is important: without it, `sudo` scrubs `SSH_AUTH_SOCK` from the
environment and any `git@github.com` operation started by the playbook prompts
for a password / gives permission-denied.

## Preconditions on the CMP

The playbook assumes ufo-simulator's earlier stages have already run:

- k0s cluster is up (`kubectl` works with the kubeconfig at
  `{{ kubeconfig_path }}`, default `/root/.kube/config`).
- KCM is installed and `management/kcm` is Ready.
- `nico-capi` has been applied (CAPI provider chart, nico-operator).
- `nico_rest_external_ip` is reachable and NICO is running there.

The full end-to-end flow is what `deploy/install.sh` orchestrates; re-running
the playbook by hand is meant for iteration on the k0rdent-apis layer without
re-running everything upstream.

## Configuration knobs

Most defaults live in the `vars:` block of [k0rdent-apis.yml](k0rdent-apis.yml)
and in [group_vars/all.yml](group_vars/all.yml). Highlights you may want to
override:

- `k0rdent_apis_dir` — checkout location (default `/opt/ufo_lab/k0rdent-apis`).
- `k0rdent_apis_provision_env` — env name under
  `scripts/post-deploy/envs/` used for the provision-manifest ConfigMap.
  Default `kind`.
- `k0rdent_apis_base_url` — external URL where Kong is reachable, injected
  into the values overrides. Default `http://10.200.0.254:30080`.
- `nico_prepovision_ssh_key_group_name` /
  `nico_prepovision_network_security_group_name` — names of the platform-admin
  CRs whose reconciled `status.id` gets pushed into the workflow-worker env
  vars.

## Creating example clusters via the API

Once the playbook has run, [k0r.sh](k0r.sh) doubles as both a script and a
library of shell helpers.

- Executed directly (`./k0r.sh` / `./k0r.sh --full`), it prints a
  cross-reference of k0rdent-apis servers against NICo machines, expected
  machines, instance-types, and instances — a quick way to see the full
  inventory across both systems and spot orphans/drift.
- Sourced (`source ./k0r.sh`), it exposes helpers you can use to poke at the
  k0rdent-apis REST surface without hand-rolling `curl`:
  - `k0r_login` — drives the mock-oauth2 login flow to mint a fresh operator JWT.
  - `k0r_token` — cached wrapper around `k0r_login` (re-mints when the JWT is
    within 60s of expiry; cache file `/tmp/k0r-token`).
  - `k0r <path> [curl args...]` — `curl` wrapper that prefixes `$BASE` and
    attaches `Authorization: Bearer $(k0r_token)`.

The examples below assume you're on the CMP (so the in-cluster `auth` /
`mock-oauth2-server` Services are reachable) and that `kubectl`, `jq`, and
`curl` are on `PATH`.

### 1. Source the helpers and pick your target

```bash
cd /opt/ufo_lab/ufo-simulator/ansible
source ./k0r.sh

# The defaults match the ufo-simulator lab; override if yours differ.
export BASE=http://10.200.0.254:30080
export REGION=local
export ORG=kind
export PROJECT=kind-main
export MT=nico-lab          # k0rdent machine-type registered by the playbook

k0r /v1/regions/global/organizations | jq
```

### 2. Create the address pools

Both cluster types below reference these pools by ID, so create them once per
region:

```bash
k0r "/v1/regions/$REGION/compute/address-pools" -X POST \
  -H 'Content-Type: application/json' -d '{
  "id": "global-default",
  "routable": false,
  "ipVersion": "IPV4",
  "prefixes": ["10.20.0.0/16"],
  "minPrefixLength": 19,
  "maxPrefixLength": 32
}'

k0r "/v1/regions/$REGION/compute/address-pools" -X POST \
  -H 'Content-Type: application/json' -d '{
  "id": "global-public",
  "routable": true,
  "ipVersion": "IPV4",
  "prefixes": ["192.168.0.0/16"],
  "minPrefixLength": 19,
  "maxPrefixLength": 32
}'
```

### 3a. Two-VPC (nico + verity) HCP cluster with per-slot NIC pinning

Exercises the multi-VPC schema (`networkSchema.vpcs[]`) with two different
fabric backends in a single cluster-type, plus a two-ethernet nodePool
where each interface is pinned to a specific physical NIC by PCI slot.

`match.pciSlot` resolves against the machine's advertised inventory via
the Link CR's `spec.peer.pciSlot`, not against on-VM `ip link` output.
The nico-core-mock helm chart advertises this ethernet inventory per
mocked machine (see
[`helm/nico-rest-mock-core/values.yaml`](../../nico-core-mock/helm/nico-rest-mock-core/values.yaml)):

```
eth0   0000:01:00.0   Mellanox ConnectX-7 (BlueField-3 integrated)   — no LLDP → no Link CR
eth1   0000:a3:00.0   Intel I350   (LLDP: leaf-0 port eth1/1)         ← ns_verity byslot target
eth2   0000:a3:00.1   Intel I350   (LLDP: leaf-0 port eth1/12)
eth3   0000:a3:00.2   Intel I350   (LLDP: leaf-1 port eth1/12)
eth4   0000:a3:00.3   Intel I350   (LLDP: leaf-1 port eth1/12)
```

Only the LLDP-attached interfaces (`eth1`..`eth4`) get a NicoMachine
Link CR, so those are the only slots `byslot` can resolve. `eth0` has
no LLDP peer → no Link CR → `match.pciSlot: 0000:01:00.0` fails
resolution. For the nico interface we therefore use the OS interface
name (`enp1s0` per systemd-predictable naming, matching what `ip link`
shows on the running worker) as the ethernet map key and skip `match`
entirely. The schema declares:

- `vpc-nico` (backend `nico`) with `net-nico` and a subnet from
  `global-default`.
- `vpc-verity` (backend `verity`) with `net-verity` and a subnet from
  `global-public`. The `verity` backend must be enabled in the region's
  fabric operator; on a nico-only lab the ClusterType create validates but
  the cluster reconcile will stall on the verity-backed Vpc until a verity
  fabric operator is present.
- Two ethernets per worker: `enp1s0` (interface-name matched, DHCP)
  attached to `net-nico`; `ns_verity` pinned by
  `match.pciSlot: 0000:a3:00.0` (mock `eth1`, LLDP-attached to `leaf-0`
  port `eth1/1`) attached to `net-verity` — this is the SNA case, where
  the Verity backend programs the required switch ports for the
  attached interface. `byslot` matching requires `connectToNetwork`,
  and `connectToNetwork` is mutually exclusive with `addresses`, so
  addressing on the verity interface is left to the Verity fabric /
  DHCP rather than pinned via `ipFromSubnet`.

```bash
k0r "/v1/regions/$REGION/compute/cluster-types" -X POST \
  -H 'Content-Type: application/json' -d @- <<EOF
{
  "id": "nico-verity-hcp",
  "displayName": "NICo + Verity HCP k0s",
  "networkSchema": {
    "vpcs": [
      { "id": "vpc-nico",   "backend": "nico"   },
      { "id": "vpc-verity", "backend": "verity" }
    ],
    "networks": [
      { "id": "net-nico",   "vpc": "vpc-nico",   "type": "evpn", "evpn": {"type":"l3vpn"} },
      { "id": "net-verity", "vpc": "vpc-verity", "type": "evpn", "evpn": {"type":"l2vpn"}, "vlan": {"id": "500" }}
    ],
    "subnets": [
      { "id": "sub-nico",
        "network": "net-nico",
        "addressPool": "/v1/regions/$REGION/compute/address-pools/global-default",
        "prefixLength": 27 },
      { "id": "sub-verity",
        "network": "net-verity",
        "addressPool": "/v1/regions/$REGION/compute/address-pools/global-public",
        "prefixLength": 29 }
    ]
  },
  "k8s": {
    "k0sNetworkProvider": "calico",
    "k0sVersion": "v1.36.1",
    "podCidrs": ["10.243.0.0/16"],
    "serviceCidrs": ["10.95.0.0/16"],
    "clusterTemplateName": "cluster-nico-0-0-0-main",
    "clusterTemplateVersion": "0.0.0-main",
    "services": [],
    "controlPlane": {
      "type": "hcp",
      "replicas": 1,
      "bootstrapConfig": {},
      "opConfig": {}
    },
    "workers": { "defaults": {} }
  },
  "nodePools": [{
    "id": "default",
    "role": "worker",
    "machineType": "/v1/regions/$REGION/infrastructure/machine-types/$MT",
    "nodeCountMin": 1,
    "nodeCountMax": 6,
    "nodeCountDefault": 3,
    "networkAttachment": {
      "ethernets": {
        "enp1s0": {
          "dhcp4": true,
          "connectToNetwork": { "name": "net-nico" }
        },
        "verity1": {
          "match": { "pciSlot": "0000:a3:00.0" },
          "dhcp4": false,
          "connectToNetwork": { "name": "net-verity" }
        }
      }
    }
  }]
}
EOF

k0r "/v1/regions/$REGION/projects/$PROJECT/compute/clusters" -X POST \
  -H 'Content-Type: application/json' -d @- <<EOF
{
  "id": "lab-nico-verity-21",
  "displayName": "Lab NICo+Verity cluster",
  "clusterType": "/v1/regions/$REGION/compute/cluster-types/nico-verity-hcp",
  "nodePools": [
    { "id": "default", "nodeCount": 3 }
  ],
  "sshAccess": [
    {
      "username": "ubuntu",
      "displayName": "ubuntu",
      "sshPublicKeys": [
        "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQCp0evjOaK8c8SKYK4r2+0BN7g+8YSvQ2n8nFgOURCyvkJqOHi1qPGZmuN0CclYVdVuZiXbWw3VxRbSW3EH736VzgY1U0JmoTiSamzLHaWsXvEIW8VCi7boli539QJP0ikJiBaNAgZILyCrVPN+A6mfqtacs1KXdZ0zlMq1BPtFciR1JTCRcVs5vP2Wwz5QtY2jMIh3aiwkePjMTQPcfmh1TkOlxYu5IbQyZ3G1ahA0mNKI9a0dtF282av/F6pwB/N1R1nEZ/9VtcN2I1mf1NW/tTHEEcTzXYo1R/8K9vlqAN8QvvGLZtZduGviNVNoNWvoxaXxDt8CPv2B2NCdQFZp /home/yar/.ssh/os-lab.pub"
      ]
    }
  ]
}
EOF
```

Field notes:

- `networkSchema.vpcs[].backend` is required (no default). It names a
  fabric operator that must exist in the region — see the OpenAPI
  `NetworkSchemaVpc.backend` description. Multi-VPC contract test:
  [cluster_deployment_create_v3_test.go::TestDerivePlans_MultiVpc](../../k0rdent-apis/services/workflow/cmd/workflow-worker/workflows/cluster_deployment_create_v3_test.go).
- `addresses[].ipFromSubnet` and `gatewayv4.gatewayFromSubnet` reference
  `networkSchema.subnets[].id`. The actual IP is allocated at cluster
  materialization from that subnet's AddressPool. The materialized
  NetworkBundle carries the resolved UFO subnet ref, which is what
  downstream reconciles binding the ethernet to the network the subnet
  belongs to.

**Version requirement.** `match.pciSlot` and `connectToNetwork` on the
v3 (HCP/CAPI) code path were added by
[KCS-1276](../../k0rdent-apis/services/workflow/cmd/workflow-worker/workflows/cluster_deployment_create_v3.go)
(commits `84fbf9160`, `ab380b93a`, `a45372183`). A workflow-worker image
built from `main` before those landed silently drops both at
`json.Unmarshal` in the v3 translator's `naEthernet` / `naMatch` structs
(the sibling translator in
[`child-workflows/providers/ufo/create_network_bundle.go`](../../k0rdent-apis/services/workflow/cmd/workflow-worker/workflows/child-workflows/providers/ufo/create_network_bundle.go)
used by 3b has always carried them). Confirm by inspecting the
materialized NetworkBundle:

```bash
sudo kubectl -n prj-kind-main get networkbundle \
  -l cluster.k0rdent.ai/name=lab-nico-verity-04 \
  -o yaml | grep -E 'pciSlot|connectToNetwork'
```

If those keys don't show up, rebuild the workflow-worker with
[`lab-inject.sh rebuild workflow-worker`](lab-inject.sh) against the
current checkout.

### 3b. Two-VPC (nico + verity) standalone BMaaS with per-slot NIC pinning

Same networkSchema + slot-pinning pattern as 3a, applied to the standalone
BMaaS flow: no `k8s` block on the cluster-type, and the deployment target
is `/compute/instance-groups` rather than `/compute/clusters`. Ensure the
project namespace carries the k0rdent labels the KCM/k0rdent-apis operators
look for:

```bash
kubectl create ns prj-kind-main --dry-run=client -o yaml | \
  kubectl label --local -f - \
    app.k0rdent.ai/managed=true \
    app.k0rdent.ai/project-namespace=true \
    k0rdent.mirantis.com/project=kind-main \
    --overwrite -o yaml | \
  kubectl apply -f -
```

```bash
k0r "/v1/regions/$REGION/compute/cluster-types" -X POST \
  -H 'Content-Type: application/json' -d @- <<EOF
{
  "id": "nico-verity-bm",
  "displayName": "NICo + Verity bare-metal (BMaaS)",
  "networkSchema": {
    "vpcs": [
      { "id": "vpc-nico",   "backend": "nico"   },
      { "id": "vpc-verity", "backend": "verity" }
    ],
    "networks": [
      { "id": "net-nico",      "vpc": "vpc-nico",   "type": "evpn", "evpn": {"type":"l3vpn"} },
      { "id": "net-verity-l3", "vpc": "vpc-verity", "type": "evpn", "evpn": {"type":"l3vpn"} },
      { "id": "net-verity-l2", "vpc": "vpc-verity", "type": "evpn", "evpn": {"type":"l2vpn"}, "vlan": {"id": "500" } }
    ],
    "subnets": [
      { "id": "sub-nico",
        "network": "net-nico",
        "addressPool": "/v1/regions/$REGION/compute/address-pools/global-default",
        "prefixLength": 27 },
      { "id": "sub-verity-l3",
        "network": "net-verity-l3",
        "addressPool": "/v1/regions/$REGION/compute/address-pools/global-public",
        "prefixLength": 29 }
    ]
  },
  "nodePools": [{
    "id": "workers",
    "role": "worker",
    "machineType": "/v1/regions/$REGION/infrastructure/machine-types/$MT",
    "nodeCountMin": 1,
    "nodeCountMax": 6,
    "nodeCountDefault": 1,
    "networkAttachment": {
      "ethernets": {
        "nico_l3_net": {
          "match": { "pciSlot": "0000:01:00.0" },
          "dhcp4": true,
          "connectToNetwork": { "name": "net-nico" }
        },
        "verity_l3_addr": {
          "match": { "pciSlot": "0000:a3:00.0" },
          "dhcp4": false,
          "addresses": [
            { "ipFromSubnet": "sub-verity-l3" }
          ]
        },
        "verity_l2_net": {
          "match": { "pciSlot": "0000:a3:00.2" },
          "dhcp4": false,
          "connectToNetwork": { "name": "net-verity-l2" }
        }
      }
    }
  }]
}
EOF

k0r "/v1/regions/$REGION/projects/$PROJECT/compute/instance-groups" -X POST \
  -H 'Content-Type: application/json' -d @- <<EOF
{
  "id": "lab-nico-verity-bm-01",
  "displayName": "Lab NICo+Verity BMaaS group",
  "clusterType": "/v1/regions/$REGION/compute/cluster-types/nico-verity-bm",
  "nodePools": [ { "nodePool": "workers", "size": 1 } ],
  "sshAccess": [
    {
      "username": "ubuntu",
      "displayName": "ubuntu",
      "sshPublicKeys": [
        "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQCp0evjOaK8c8SKYK4r2+0BN7g+8YSvQ2n8nFgOURCyvkJqOHi1qPGZmuN0CclYVdVuZiXbWw3VxRbSW3EH736VzgY1U0JmoTiSamzLHaWsXvEIW8VCi7boli539QJP0ikJiBaNAgZILyCrVPN+A6mfqtacs1KXdZ0zlMq1BPtFciR1JTCRcVs5vP2Wwz5QtY2jMIh3aiwkePjMTQPcfmh1TkOlxYu5IbQyZ3G1ahA0mNKI9a0dtF282av/F6pwB/N1R1nEZ/9VtcN2I1mf1NW/tTHEEcTzXYo1R/8K9vlqAN8QvvGLZtZduGviNVNoNWvoxaXxDt8CPv2B2NCdQFZp /home/yar/.ssh/os-lab.pub"
      ]
    }
  ]
}
EOF
```

Same interface-selection rules as 3a apply: the nico interface uses
its OS-predictable name (`enp1s0`) as the ethernet-map key because the
`0000:01:00.0` NIC (mock `eth0`, Mellanox) has no LLDP peer → no Link
CR → `byslot` can't resolve it. The verity side exercises two
byslot-pinned interfaces, both against LLDP-attached slots that carry a
Link CR (per the mock inventory in
[`helm/nico-rest-mock-core/values.yaml`](../../nico-core-mock/helm/nico-rest-mock-core/values.yaml)):

- `verity-l3-addr` → slot `0000:a3:00.0` (mock `eth1`, leaf-0 port
  `eth1/1`) on the l3vpn network `net-verity-l3`, addressed via
  `addresses[].ipFromSubnet: "sub-verity-l3"`.
- `verity-l2-net` → slot `0000:a3:00.2` (mock `eth3`, leaf-1 port
  `eth1/1`) on the l2vpn network `net-verity-l2` (VLAN 500), joined via
  `connectToNetwork: { name: "net-verity-l2" }` with no `addresses` —
  L2 addressing is left to the fabric / DHCP.

Unlike 3a's HCP path, the instance-group translator
([`create_network_bundle.go`](../../k0rdent-apis/services/workflow/cmd/workflow-worker/workflows/child-workflows/providers/ufo/create_network_bundle.go))
does not enforce mutual exclusion between `match.pciSlot` and
`addresses[]`: `translateMatch` and `translateAddresses` are emitted
independently, so byslot-pinning coexists with subnet-allocated
addressing on the same ethernet. `verity` must be an enabled fabric
backend in the region for the `vpc-verity` reconcile to complete.

The instance-group flow uses the sibling translator in
[`child-workflows/providers/ufo/create_network_bundle.go`](../../k0rdent-apis/services/workflow/cmd/workflow-worker/workflows/child-workflows/providers/ufo/create_network_bundle.go)
rather than the v3 inline one, so the KCS-1276 version requirement
called out for 3a does not apply here — `match.pciSlot` and
`connectToNetwork` have always been carried through in this code path.

## Iterating on Go changes

Two ways to test a code change against a running lab, depending on how far
down the deployment path you want the change to travel.

### Option A: inject a fresh binary with [lab-inject.sh](lab-inject.sh) (fast, recommended)

Skips the image build/push/redeploy loop while keeping full pod
fidelity — projected ServiceAccount JWT, NetworkPolicy pod-identity,
cgroup memory/CPU limits, and anything else that only holds when the
process runs inside the pod. It compiles the source, drops the binary
on the CMP (the k0s node), and strategic-merge-patches the Deployment
to run *your* binary instead of the image's baked-in one. The pod
otherwise starts exactly as a fresh rollout would.

**How it works.** The k0rdent-apis images entrypoint is
`sh -c "exec ${BINARY}"` with `ENV BINARY=/app/${CMD_NAME}` baked into
the image (see [Dockerfile.go-service](../../k0rdent-apis/build/Dockerfile.go-service)).
Setting `BINARY=/dev-bin/<dep>` at the Pod level overrides that
default, and a hostPath volume mounted at `/dev-bin/` reads binaries
from `/opt/lab-binaries/` on the CMP.

**Iteration loop:**

```bash
INJ=/opt/ufo_lab/ufo-simulator/ansible/lab-inject.sh

sudo $INJ rebuild iam            # go build → /opt/lab-binaries/iam,
                                 # patch deploy/iam on first call,
                                 # rollout restart, wait for readiness
# ...edit code, run rebuild again...

sudo $INJ stop    iam            # reverse the patch, rollout, delete binary
```

`rebuild` is idempotent — the first call adds the patch; subsequent
calls skip the patch step (it's a no-op) and just recompile +
`kubectl rollout restart` so the pod re-execs with the fresh binary.

### Option B: rebuild the image, push to zot, redeploy

Slower loop (roughly a minute per iteration with a warm cache) but
exercises the same image-pull + container-startup path a production
deployment would. Useful when the change touches the `Dockerfile`, image
entrypoint, resource limits, or anything that only manifests as a
containerized process.

**One-time setup** — deploy the in-cluster OCI registry (namespace `zot`,
external NodePort 30500):

```bash
cd /opt/ufo_lab/ufo-simulator/ansible
sudo -E ansible-playbook -i inventory.yml zot.yml --limit cmp01
```

Configure k0s's containerd to trust zot over plain HTTP (not yet
automated in the playbook). Add to the `[plugins."io.containerd.grpc.v1.cri".registry]`
block in [k0s.yml](k0s.yml) and re-run the playbook:

```toml
[plugins."io.containerd.grpc.v1.cri".registry.mirrors."10.200.0.254:30500"]
  endpoint = ["http://10.200.0.254:30500"]
[plugins."io.containerd.grpc.v1.cri".registry.configs."10.200.0.254:30500".tls]
  insecure_skip_verify = true
```

If you `docker push` from the CMP or your laptop, the docker daemon also
needs zot in its insecure list — add `"insecure-registries":
["10.200.0.254:30500"]` to `/etc/docker/daemon.json` and restart docker.

**Iteration loop** (per-service `docker-build` targets in the k0rdent-apis
Makefiles produce a `<binary>:latest` image locally):

```bash
cd /opt/ufo_lab/k0rdent-apis
make -C services/iam docker-build              # → iam:latest

docker tag  iam:latest 10.200.0.254:30500/k0r-iam:dev
docker push 10.200.0.254:30500/k0r-iam:dev

kubectl -n k0rdent-apis set image deploy/iam iam=10.200.0.254:30500/k0r-iam:dev
kubectl -n k0rdent-apis rollout restart deploy/iam
```

Special-case build targets: `services/workflow` has two binaries
(`docker-build-api`, `docker-build-worker`); `reconciler`, `kong`, and
`k0r-tools` build from the repo root (`make docker-build-reconciler`,
`make docker-build-kong`, `make docker-build-k0r-tools`).

**Persistence.** `kubectl set image` gets clobbered the next time
`k0rdent-apis.yml` runs (helm reverts the tag). To keep an image change
across playbook runs, bump the tag in
[templates/k0rdent-apis/values-socks-overrides.yaml.j2](templates/k0rdent-apis/values-socks-overrides.yaml.j2),
or check the underlying source change in as a patch under
[files/k0rdent-apis/patches/](files/k0rdent-apis/patches/).

## Troubleshooting

- **Patch fails to apply**: the vendored patches under
  [files/k0rdent-apis/patches/](files/k0rdent-apis/patches/) target specific
  upstream lines. If upstream drifts, the shell task fails loudly with git's
  own error output. Fix: refresh the patches against the newer upstream.
- **`Ensure required NICO resource IDs …` assert fails**: the NICO REST
  backend doesn't have an object with the configured name. The fail_msg lists
  the names it *does* have — check whether your platform-admin CR reconciled
  successfully (`kubectl -n platform-admin get sshkeygroup / networksecuritygroup -o yaml`).
- **provision-apply Job did not succeed**: inspect the container output —
  `kubectl -n k0rdent-apis logs job/provision-apply`.
- **nico-sync failed on unavailable servers**: `kubectl -n k0rdent-apis logs
  deploy/workflow-worker | grep expected-machine` to see why individual
  servers didn't reach `state=available`.
