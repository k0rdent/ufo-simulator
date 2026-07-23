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
