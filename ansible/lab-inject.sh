#!/usr/bin/env bash
# lab-inject.sh — Overlay a freshly-compiled binary into an in-cluster
# k0rdent-apis Deployment via a hostPath volume, so the pod runs your
# local code without rebuilding + pushing the container image.
#
# The process runs *as the pod* — real projected ServiceAccount JWT, real
# NetworkPolicy pod-identity, real cgroup limits — at the cost of a pod
# restart per rebuild.
#
# How it works:
#   • Discovers the go package for the Deployment by walking
#     services/*/cmd/<name>/main.go.
#   • Compiles a static linux/amd64 binary into $BIN_DIR/<dep> on the
#     CMP (the k0s node — hostPath source lives here).
#   • Strategic-merge-patches the Deployment: adds a hostPath volume
#     mounted at $MOUNT_PATH, sets container env BINARY=$MOUNT_PATH/<dep>.
#   • The image's ENTRYPOINT `sh -c "exec ${BINARY}"` (see
#     k0rdent-apis/build/Dockerfile.go-service) picks that up on restart,
#     running your compiled binary instead of the baked-in one.
#
# Idempotent — `rebuild` handles both first-time and repeat invocations:
# strategic-merge patches merge by name key, so re-applying the same
# volume/mount/env entries is a no-op. The interesting step is the fresh
# binary + `rollout restart`.
#
# Multi-binary services (workflow → workflow + workflow-worker
# Deployments) work naturally: each Deployment gets its own binary
# under $BIN_DIR/<dep>; both pods can share the same hostPath dir.
#
# It also toggles the workflow stack's mock mode (`mock on|off`), which the
# k0rdent-apis e2e suites need — see the "Mock mode" section further down.
#
# Usage:
#   sudo ./lab-inject.sh rebuild <deployment> [--pkg PATH]
#   sudo ./lab-inject.sh stop    <deployment>
#   sudo ./lab-inject.sh mock    on|off|status
#   sudo ./lab-inject.sh status

set -euo pipefail

: "${NAMESPACE:=k0rdent-apis}"
: "${K0RDENT_APIS_DIR:=/opt/ufo_lab/k0rdent-apis}"
: "${BIN_DIR:=/opt/lab-binaries}"
: "${MOUNT_PATH:=/dev-bin}"
: "${KUBECONFIG:=/root/.kube/config}"
export KUBECONFIG

# Mock mode lives in the workflow chart's shared ConfigMap, which BOTH the
# workflow-api and workflow-worker Deployments consume via envFrom (see
# k0rdent-apis/deploy/helm/charts/services/workflow/templates/configmap-shared.yaml).
: "${WORKFLOW_CONFIGMAP:=workflow-config}"
WORKFLOW_DEPLOYMENTS=(workflow workflow-worker)

log() { printf '%s\n' "$*" >&2; }
ok()  { log "✓ $*"; }
die() { log "✗ $*"; exit 1; }

kc() { kubectl -n "$NAMESPACE" "$@"; }

usage() {
  cat >&2 <<EOF
usage:
  $0 rebuild <deployment> [--pkg PATH]
  $0 stop    <deployment>
  $0 mock    on|off|status
  $0 status

env overrides:
  NAMESPACE           k8s namespace          (default: $NAMESPACE)
  K0RDENT_APIS_DIR    local checkout root    (default: $K0RDENT_APIS_DIR)
  BIN_DIR             hostPath source dir    (default: $BIN_DIR)
  MOUNT_PATH          binary mount path      (default: $MOUNT_PATH)
  WORKFLOW_CONFIGMAP  mock-mode ConfigMap    (default: $WORKFLOW_CONFIGMAP)
  KUBECONFIG                                 (default: $KUBECONFIG)
EOF
  exit 1
}

# ── Discovery ───────────────────────────────────────────────────

find_pkg() {
  local dep=$1
  local match
  match=$(find "$K0RDENT_APIS_DIR/services" -maxdepth 3 -type d \
    -name "$dep" -path "*/cmd/*" 2>/dev/null | head -1)
  [[ -n $match ]] || return 1
  echo "./${match#"$K0RDENT_APIS_DIR"/}"
}

# Chart convention: container name = deployment name. Fall back to
# containers[0] so a future rename doesn't silently break us.
container_name() {
  local dep=$1
  kc get "deploy/$dep" -o json | jq -r --arg n "$dep" '
    (.spec.template.spec.containers | map(select(.name == $n)) | .[0].name)
    // .spec.template.spec.containers[0].name
  '
}

# True iff the Deployment already has our dev-binary volume — used so
# `rebuild` can skip re-patching (though the strategic-merge patch is
# itself idempotent, this keeps the log line honest).
is_injected() {
  local dep=$1
  local n
  n=$(kc get "deploy/$dep" -o json 2>/dev/null | jq '
    [.spec.template.spec.volumes[]? | select(.name == "dev-binary")] | length
  ')
  [[ "${n:-0}" -gt 0 ]]
}

# Verify PID 1 is running the binary we just built. Two checks:
#
#   1. `readlink /proc/1/exe` == $MOUNT_PATH/$dep — the BINARY env
#      override is in effect (entrypoint execed the injected path, not
#      the image's baked-in default).
#
#   2. `[ /proc/1/exe -ef $MOUNT_PATH/$dep ]` — same device+inode after
#      following both symlinks. Catches "stale binary" cases the path
#      check misses: go build writes via os.Rename, so after a build
#      the file at $MOUNT_PATH/$dep has a fresh inode while a not-yet-
#      restarted PID 1 still holds the old (now-unlinked) one; -ef
#      returns false in that case.
#
# We deliberately don't use bare `stat -c %i /proc/1/exe` — coreutils
# stat does NOT follow symlinks by default, so it would return the
# procfs symlink's own inode (on procfs, device 0xN) rather than the
# target's. `-ef` follows both sides and works under bash and busybox.
verify_running_binary() {
  local dep=$1
  local expected="$MOUNT_PATH/$dep"
  # One exec, two lines out: symlink target + identity result.
  local out
  out=$(kc exec "deploy/$dep" -- sh -c '
    readlink /proc/1/exe
    if [ /proc/1/exe -ef '"$expected"' ]; then echo match; else echo mismatch; fi
  ' 2>/dev/null) || die "could not exec into deploy/$dep to verify"

  local exe_path identity
  { IFS= read -r exe_path
    IFS= read -r identity; } <<<"$out"

  local deleted=""
  if [[ "$exe_path" == *" (deleted)" ]]; then
    exe_path="${exe_path% (deleted)}"
    deleted=" (deleted)"
  fi
  if [[ "$exe_path" != "$expected" ]]; then
    die "PID 1 exe is '$exe_path', expected '$expected' — pod is running the image's baked-in binary (patch didn't take, or you're looking at an older ReplicaSet's pod)"
  fi
  if [[ "$identity" != "match" ]]; then
    die "PID 1 is running a stale copy of $expected — the file was replaced by go build but the process still holds the old inode. Delete the pod (or wait for the rolling restart to complete) to pick up the fresh binary."
  fi
  ok "PID 1 exe → $exe_path$deleted (same file as $expected on disk)"
}

binary() { echo "$BIN_DIR/$1"; }

# ── Build & patch ───────────────────────────────────────────────

build_binary() {
  local pkg=$1 out=$2
  log "compiling $pkg → $out"
  # Static linux/amd64: matches the alpine image's runtime, no cgo deps.
  ( cd "$K0RDENT_APIS_DIR" && \
    CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
    go build -o "$out" "$pkg" )
  chmod 0755 "$out"
}

# Strategic-merge patch — volumes/volumeMounts/env merge by name key, so
# re-applying is a no-op.
apply_inject_patch() {
  local dep=$1 cname=$2
  kc patch "deploy/$dep" --type=strategic --patch "
spec:
  template:
    spec:
      volumes:
        - name: dev-binary
          hostPath:
            path: $BIN_DIR
            type: Directory
      containers:
        - name: $cname
          volumeMounts:
            - name: dev-binary
              mountPath: $MOUNT_PATH
              readOnly: true
          env:
            - name: BINARY
              value: $MOUNT_PATH/$dep
" >/dev/null
}

# \$patch: delete removes the merge-key entry cleanly; dropping BINARY
# from container env lets the image's Dockerfile default
# (ENV BINARY=/app/\${CMD_NAME}) take effect again.
#
# Merge keys per field (from k8s API types.go patchMergeKey tags):
#   • .spec.template.spec.volumes                 → name
#   • .spec.template.spec.containers              → name
#   • containers[].env                            → name
#   • containers[].volumeMounts                   → mountPath  ← NOT name
# Strategic-merge \$patch: delete has to identify entries by their merge
# key, so the volumeMounts delete uses mountPath.
revert_inject_patch() {
  local dep=$1 cname=$2
  kc patch "deploy/$dep" --type=strategic --patch "
spec:
  template:
    spec:
      volumes:
        - name: dev-binary
          \$patch: delete
      containers:
        - name: $cname
          volumeMounts:
            - mountPath: $MOUNT_PATH
              \$patch: delete
          env:
            - name: BINARY
              \$patch: delete
" >/dev/null
}

# ── Mock mode ───────────────────────────────────────────────────
#
# MOCK_MODE makes the workflow stack synthesize provisioning outcomes
# instead of driving NICo/UFO: cluster, instance-group, network and
# vpc-peering workflows short-circuit to a deterministic "done" and fire
# their lifecycle callback immediately. Most k0rdent-apis e2e suites are
# written against that boundary — they wait ~90s for a cluster to reach
# `active`, which a real provision on this lab will never do.
#
# The authoritative reader is the workflow-API, not the worker: its
# consumer handler stamps `mockMode` onto each execution row as it is
# created (`req.MockMode = req.MockMode || h.mockMode`, see
# k0rdent-apis/services/workflow/internal/handler/consumer.go). The worker
# reads MOCK_MODE too (kubeconfig activities), so both Deployments are
# restarted. Patching only the worker's env would NOT flip anything.
#
# We patch the shared ConfigMap rather than the Deployments because that is
# the single value the chart renders into both. A `helm upgrade` — i.e. a
# re-run of k0rdent-apis.yml — reverts it; to make it stick, parameterize
# `workflow.config.worker.mockMode` in
# templates/k0rdent-apis/values-socks-overrides.yaml.j2 instead.

mock_current() {
  kc get "cm/$WORKFLOW_CONFIGMAP" -o jsonpath='{.data.MOCK_MODE}' 2>/dev/null || true
}

# What the running pods actually have, which is what matters — a ConfigMap
# edit does nothing until the Deployments restart (envFrom is resolved once,
# at container start).
#
# Retried, because `kubectl rollout status` returns while the old replica is
# still terminating and `kubectl exec deploy/<x>` may select that pod — a
# single attempt right after a restart reports a spurious failure. stderr is
# captured separately rather than folded into the value, so a genuine exec
# failure is quoted instead of being flattened into an opaque marker.
mock_running() {
  local dep=$1 out attempt errf
  errf=$(mktemp)
  for attempt in 1 2 3 4 5; do
    if out=$(kc exec "deploy/$dep" -- sh -c 'printf %s "${MOCK_MODE-<unset>}"' 2>"$errf"); then
      rm -f "$errf"
      printf '%s' "$out"
      return 0
    fi
    sleep 2
  done
  local err; err=$(head -1 "$errf")
  rm -f "$errf"
  printf '<exec failed: %s>' "${err:-no stderr}"
}

cmd_mock() {
  local action=$1
  local want

  case "$action" in
    on)  want=true  ;;
    off) want=false ;;
    status)
      printf '%-24s %s\n' "cm/$WORKFLOW_CONFIGMAP" "MOCK_MODE=$(mock_current)"
      local dep
      for dep in "${WORKFLOW_DEPLOYMENTS[@]}"; do
        kc get "deploy/$dep" >/dev/null 2>&1 || { printf '%-24s %s\n' "deploy/$dep" "(absent)"; continue; }
        printf '%-24s %s\n' "deploy/$dep" "MOCK_MODE=$(mock_running "$dep") (in the running pod)"
      done
      return 0
      ;;
    *) die "mock takes on|off|status (got '$action')" ;;
  esac

  kc get "cm/$WORKFLOW_CONFIGMAP" >/dev/null 2>&1 \
    || die "no cm/$WORKFLOW_CONFIGMAP in ns/$NAMESPACE — is k0rdent-apis installed? (override with WORKFLOW_CONFIGMAP=)"
  local dep
  for dep in "${WORKFLOW_DEPLOYMENTS[@]}"; do
    kc get "deploy/$dep" >/dev/null 2>&1 || die "no deploy/$dep in ns/$NAMESPACE"
  done

  if [[ "$want" == "true" ]]; then
    log "WARNING: mock mode makes ALL provisioning synthetic — no NICo/UFO object is"
    log "         created or deleted while it is on. Do not run nico-sync or trust the"
    log "         lab's inventory until you turn it back off."
  fi

  local current; current=$(mock_current)
  if [[ "$current" == "$want" ]]; then
    log "cm/$WORKFLOW_CONFIGMAP already has MOCK_MODE=$want — restarting anyway so the pods are known to match"
  else
    log "patching cm/$WORKFLOW_CONFIGMAP: MOCK_MODE=${current:-<unset>} → $want"
    kc patch "cm/$WORKFLOW_CONFIGMAP" --type merge \
      -p "{\"data\":{\"MOCK_MODE\":\"$want\"}}" >/dev/null
  fi

  # envFrom is read once at container start, so a restart is not optional.
  for dep in "${WORKFLOW_DEPLOYMENTS[@]}"; do
    kc rollout restart "deploy/$dep" >/dev/null
  done
  for dep in "${WORKFLOW_DEPLOYMENTS[@]}"; do
    kc rollout status "deploy/$dep" --timeout=180s
  done

  # Verify against the pods rather than the ConfigMap: an injected binary, a
  # stray `kubectl set env`, or a half-finished rollout would all leave the
  # ConfigMap saying one thing and the process seeing another.
  local failed=0
  for dep in "${WORKFLOW_DEPLOYMENTS[@]}"; do
    local got; got=$(mock_running "$dep")
    if [[ "$got" == "$want" ]]; then
      ok "deploy/$dep MOCK_MODE=$got"
    else
      log "✗ deploy/$dep reports MOCK_MODE=$got, expected $want"
      failed=1
    fi
  done
  [[ $failed -eq 0 ]] \
    || die "mock $action did not take effect in every pod — check for an explicit MOCK_MODE env entry on the Deployment (it overrides envFrom): kubectl -n $NAMESPACE get deploy -o yaml | grep -A2 MOCK_MODE"

  ok "workflow stack is running with MOCK_MODE=$want"
}

# ── Commands ────────────────────────────────────────────────────

cmd_rebuild() {
  local dep=$1; shift
  local pkg_override=""
  while [[ $# -gt 0 ]]; do
    case $1 in
      --pkg) pkg_override=$2; shift 2 ;;
      *) die "unknown flag: $1" ;;
    esac
  done

  [[ -d $K0RDENT_APIS_DIR ]] || die "checkout not found: $K0RDENT_APIS_DIR"
  command -v go >/dev/null 2>&1 \
    || die "go not on PATH — Go is installed as part of ufo.yml. Re-run: sudo -E ansible-playbook -i inventory.yml ufo.yml --limit cmp01"
  kc get "deploy/$dep" >/dev/null 2>&1 || die "no deploy/$dep in ns/$NAMESPACE"

  local pkg
  if [[ -n $pkg_override ]]; then
    pkg=$pkg_override
  else
    pkg=$(find_pkg "$dep") || die "no cmd/<name>/main.go for '$dep' — pass --pkg"
  fi
  [[ -f "$K0RDENT_APIS_DIR/${pkg#./}/main.go" ]] || die "resolved pkg '$pkg' has no main.go"

  local cname; cname=$(container_name "$dep")
  [[ -n $cname && "$cname" != "null" ]] || die "could not resolve container name for deploy/$dep"

  mkdir -p "$BIN_DIR"
  build_binary "$pkg" "$(binary "$dep")"

  if is_injected "$dep"; then
    log "deploy/$dep already patched — rollout restart to pick up new binary"
    kc rollout restart "deploy/$dep" >/dev/null
  else
    log "patching deploy/$dep (container=$cname) → BINARY=$MOUNT_PATH/$dep"
    apply_inject_patch "$dep" "$cname"
    # Strategic-merge patch changes the pod template hash → new
    # ReplicaSet, so no explicit restart needed here.
  fi
  kc rollout status "deploy/$dep" --timeout=120s
  verify_running_binary "$dep"
  ok "deploy/$dep is running $(binary "$dep")"
}

cmd_stop() {
  local dep=$1
  kc get "deploy/$dep" >/dev/null 2>&1 || die "no deploy/$dep in ns/$NAMESPACE"

  if is_injected "$dep"; then
    local cname; cname=$(container_name "$dep")
    log "reverting deploy/$dep patch (container=$cname)"
    revert_inject_patch "$dep" "$cname"
    kc rollout status "deploy/$dep" --timeout=120s
  else
    ok "deploy/$dep not injected — nothing to revert"
  fi
  rm -f "$(binary "$dep")"
  ok "removed $(binary "$dep")"
}

# Enumerate Deployments in $NAMESPACE that carry our dev-binary volume.
# No state files to consult — the cluster itself is the source of truth.
cmd_status() {
  echo "mock mode: MOCK_MODE=$(mock_current) in cm/$WORKFLOW_CONFIGMAP  ($0 mock status for the running pods)"
  echo

  local list
  list=$(kc get deploy -o json | jq -r '
    .items[]
    | select(.spec.template.spec.volumes[]?.name == "dev-binary")
    | .metadata.name
  ')
  if [[ -z $list ]]; then
    echo "no services injected."
    return
  fi
  printf '%-24s %-16s %s\n' DEPLOYMENT CONTAINER BINARY
  local dep cname bin info
  while IFS= read -r dep; do
    cname=$(container_name "$dep")
    bin=$(binary "$dep")
    if [[ -f $bin ]]; then
      info="$bin ($(stat -c '%s bytes, %y' "$bin"))"
    else
      info="$bin (MISSING — pod will crashloop)"
    fi
    printf '%-24s %-16s %s\n' "$dep" "$cname" "$info"
  done <<<"$list"
}

# ── Dispatch ────────────────────────────────────────────────────

case "${1:-}" in
  rebuild) shift; [[ $# -ge 1 ]] || usage; cmd_rebuild "$@" ;;
  stop)    shift; [[ $# -eq 1 ]] || usage; cmd_stop    "$1" ;;
  mock)    shift; [[ $# -eq 1 ]] || usage; cmd_mock    "$1" ;;
  status)  shift; cmd_status ;;
  *) usage ;;
esac
