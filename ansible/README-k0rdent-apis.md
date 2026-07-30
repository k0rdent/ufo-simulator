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

- `k0rdent_apis_repo` — git URL (default upstream on GitHub).
- `k0rdent_apis_dir` — checkout location (default `/opt/ufo_lab/k0rdent-apis`).
- `k0rdent_apis_provision_env` — env name under
  `scripts/post-deploy/envs/` used for the provision-manifest ConfigMap.
  Default `kindmock`.
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
export ORG=kindmock
export PROJECT=kindmock-main
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

### 3a. HCP-managed k0s cluster (CAPI flow)

The CAPI provider must already be installed via KCM — see
<https://github.com/k0rdent/ncx-ailab/tree/main/k0rdent/kcm>. This registers a
`nico-hcp` cluster-type with a hosted control plane and a single `default`
worker nodepool backed by the `$MT` machine-type, then instantiates a cluster:

```bash
k0r "/v1/regions/$REGION/compute/cluster-types" -X POST \
  -H 'Content-Type: application/json' -d @- <<EOF
{
  "id": "nico-hcp",
  "displayName": "NICo HCP k0s",
  "networkSchema": {
    "networks": [
      { "id": "ns",     "type": "evpn", "evpn": {"type":"l3vpn"} },
      { "id": "public", "type": "evpn", "evpn": {"type":"l3vpn"} }
    ],
    "subnets": [
      { "id": "ns-subnet",
        "network": "ns",
        "addressPool": "/v1/regions/$REGION/compute/address-pools/global-default",
        "prefixLength": 27 },
      { "id": "public-subnet",
        "network": "public",
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
    "workers": {
      "defaults": {
        "preStartCommands": [
          "sudo useradd -G sudo -s /bin/bash -d /home/rescue -p \$(openssl passwd -1 rescue) rescue",
          "sudo mkdir -vp /home/rescue/.ssh/",
          "sudo chown rescue /home/rescue/.ssh/authorized_keys",
          "/usr/bin/sleep 180"
        ],
        "files": [{
          "path": "/etc/ssh/sshd_config.d/99-cloudimg-settings.conf",
          "permissions": "0644",
          "content": "PasswordAuthentication yes\n"
        }]
      }
    }
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
          "connectToNetwork": "ns"
        }
      }
    }
  }]
}
EOF

k0r "/v1/regions/$REGION/projects/$PROJECT/compute/clusters" -X POST \
  -H 'Content-Type: application/json' -d @- <<EOF
{
  "id": "lab-nico-01",
  "displayName": "Lab NICo cluster",
  "clusterType": "/v1/regions/$REGION/compute/cluster-types/nico-hcp",
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

Grab the child-cluster kubeconfig once the cluster is Ready:

```bash
kubectl -n prj-kindmock-main get secret lab-nico-01-kubeconfig \
  -o jsonpath='{.data.value}' | base64 -d > /tmp/lab-nico-child.kubeconfig

kubectl --kubeconfig /tmp/lab-nico-child.kubeconfig get nodes -o wide
```

### 3b. Standalone BMaaS instance-group

For the bare-metal-only flow (no k0s control plane, k0rdent just provisions
machines into a NICo instance-group), first make sure the project namespace
carries the labels the KCM/k0rdent-apis operators look for:

```bash
kubectl create ns prj-kindmock-main --dry-run=client -o yaml | \
  kubectl label --local -f - \
    app.k0rdent.ai/managed=true \
    app.k0rdent.ai/project-namespace=true \
    k0rdent.mirantis.com/project=kindmock-main \
    --overwrite -o yaml | \
  kubectl apply -f -
```

Then register a cluster-type that only declares a `workers` nodepool (no `k8s`
block) and instantiate an instance-group against it:

```bash
k0r "/v1/regions/$REGION/compute/cluster-types" -X POST \
  -H 'Content-Type: application/json' -d @- <<EOF
{
  "id": "nico-bm",
  "displayName": "NICo bare-metal (BMaaS)",
  "networkSchema": {
    "networks": [
      { "id": "ns",     "type": "evpn", "evpn": {"type":"l3vpn"} },
      { "id": "public", "type": "evpn", "evpn": {"type":"l3vpn"} }
    ],
    "subnets": [
      { "id": "ns-subnet",
        "network": "ns",
        "addressPool": "/v1/regions/$REGION/compute/address-pools/global-default",
        "prefixLength": 27 },
      { "id": "public-subnet",
        "network": "public",
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
        "enp1s0": {
          "dhcp4": true,
          "connectToNetwork": { "name": "ns" }
        }
      }
    }
  }]
}
EOF

k0r "/v1/regions/$REGION/projects/$PROJECT/compute/instance-groups" -X POST \
  -H 'Content-Type: application/json' -d @- <<EOF
{
  "id": "lab-bm-01",
  "displayName": "Lab BMaaS group",
  "clusterType": "/v1/regions/$REGION/compute/cluster-types/nico-bm",
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

## Iterating on Go changes

Two ways to test a code change against a running lab, depending on how far
down the deployment path you want the change to travel.

### Option A: run the service locally with [lab-extract.sh](lab-extract.sh) (fast, recommended)

Scales an in-cluster `Deployment` to 0, hijacks its `Service` endpoints so
cluster traffic keeps flowing to it, and runs `go run` against the
`k0rdent-apis` checkout at `/opt/ufo_lab/k0rdent-apis` on the CMP. No image
build, no push, no rollout — the loop is `edit → ^C → run again`.

**Iteration loop** (all on the CMP, as root):

```bash
LAB=/opt/ufo_lab/ufo-simulator/ansible/lab-extract.sh

sudo $LAB start iam            # scales deploy/iam to 0, hijacks svc/iam,
                               # captures env + /etc/k0rdent-ai/ from the pod
sudo $LAB run   iam            # unshare -m + bind-mount + go run ./services/iam/cmd/iam

# ...edit code, ^C to stop, `sudo $LAB run iam` again...

sudo $LAB stop  iam            # restores selector, scales replicas back
```

`run` sets up a private mount namespace so the local process sees an
overlay `/etc/hosts` (cluster DNS resolves to `ClusterIP`s) and
`/etc/k0rdent-ai/` (file-projected secrets and config match the pod's
view). Nothing on the CMP host is modified. See the header comment in
[lab-extract.sh](lab-extract.sh) for the mechanics.

**Multiple services at once** — port selection is automatic. `start` probes
upward from the Service's `containerPort` and picks the first free port
that no other extract has claimed:

```bash
sudo $LAB start iam            # picks 8080 (or next free)
sudo $LAB start compute        # picks 8081
sudo $LAB start organizations  # picks 8082
sudo $LAB status               # shows what's swapped in
# then `sudo $LAB run <svc>` in separate shells.
```

Cross-service calls between two locally-run services still flow through
Kong internal — Kong dials `iam.k0rdent-apis.svc:80` → hijacked
`Endpoints` → `<node-ip>:<picked-port>` → your local process.

**What's extractable** — anything with a Go binary under
`k0rdent-apis/services/*/cmd/`: `auth`, `iam`, `organizations`, `compute`,
`infrastructure`, `notifications`, `workflow` (workflow-api),
`workflow-worker`, and `reconciler`. Third-party components (Kong,
Keycloak, Postgres, Temporal, Mailpit) can't be extracted — the script
refuses when no matching Go package exists.

**Debugger.** `sudo $LAB start <svc>` prints a raw `unshare -m ...`
incantation you can copy-modify to inject `dlv exec`/`strace`/etc. in
place of the plain `go run`.

### Option B: inject a fresh binary with [lab-inject.sh](lab-inject.sh) (fidelity-preserving)

When the process's *environment* rather than its logic is what you're
testing — projected ServiceAccount JWT, NetworkPolicy pod-identity,
cgroup memory/CPU limits, or anything else that only holds when the
process runs inside the pod — swap `lab-extract` for `lab-inject`. It
compiles the same source, drops the binary on the CMP (the k0s node),
and strategic-merge-patches the Deployment to run *your* binary
instead of the image's baked-in one. The pod otherwise starts exactly
as a fresh rollout would.

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

### Option C: rebuild the image, push to zot, redeploy

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
