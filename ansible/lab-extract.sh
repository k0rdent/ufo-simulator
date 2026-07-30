#!/usr/bin/env bash
# lab-extract.sh — swap an in-cluster k0rdent-apis Deployment for a locally-run
# `go run` on cmp01, so a rebuild-restart loop drops to a single `go run`.
#
# Design:
#   • Works against the cluster + checkout that k0rdent-apis.yml provisions:
#     kubeconfig at /root/.kube/config, checkout at /opt/ufo_lab/k0rdent-apis.
#   • Nothing about the service catalog is hard-coded — the Deployment is
#     looked up in the cluster, the go package is found by walking
#     `services/*/cmd/<name>` in the checkout, and mode (api vs worker) is
#     inferred from whether a Service with the same name exists.
#   • API services (Service exists): pod's Endpoints are hijacked to point at
#     the node IP + local port, so in-cluster clients (kong-internal, other
#     services) keep resolving <name>.<ns>.svc and land on the local process.
#   • Worker services (no Service): just capture env, scale to 0, run locally.
#   • DNS for cluster names (kong-internal, workflow-api.k0rdent-apis.svc, …)
#     is provided by a per-process /etc/hosts bind-mount inside a private
#     mount namespace — set up by `lab-extract.sh run`. The host's real
#     /etc/hosts is untouched. This covers env-configured URLs *and*
#     hardcoded defaults in code (e.g. the `http://kong-internal:8100`
#     fallback in services/*/internal/config/*.go), which pure env
#     rewriting can't reach.
#   • File-projected config / secrets under /etc/k0rdent-ai/<svc>/ (chart
#     file-mode via .Values.<svc>.fileSecretRef; e.g. INFRASTRUCTURE_DB_URL
#     lives at /etc/k0rdent-ai/infrastructure/secrets/) are streamed from
#     the running pod with `kubectl exec deploy/<x> -- tar` at start time,
#     then bind-mounted onto /etc/k0rdent-ai inside the same mount
#     namespace — so file references like *_URL_FILE resolve identically.
#
# Usage:
#   sudo ./lab-extract.sh start <deployment> [--local-port N] [--pkg PATH]
#   sudo ./lab-extract.sh run   <deployment>   # unshare + bind-mount + go run
#   sudo ./lab-extract.sh stop  <deployment>
#   sudo ./lab-extract.sh status

set -euo pipefail

: "${NAMESPACE:=k0rdent-apis}"
: "${K0RDENT_APIS_DIR:=/opt/ufo_lab/k0rdent-apis}"
: "${STATE_DIR:=/var/lib/lab-extract}"
: "${KUBECONFIG:=/root/.kube/config}"
export KUBECONFIG

log()  { printf '%s\n' "$*" >&2; }
ok()   { log "✓ $*"; }
die()  { log "✗ $*"; exit 1; }

kc() { kubectl -n "$NAMESPACE" "$@"; }

usage() {
  cat >&2 <<EOF
usage:
  $0 start <deployment> [--local-port N] [--pkg PATH]
  $0 run   <deployment>
  $0 stop  <deployment>
  $0 status

env overrides:
  NAMESPACE         k8s namespace         (default: $NAMESPACE)
  K0RDENT_APIS_DIR  local checkout root   (default: $K0RDENT_APIS_DIR)
  STATE_DIR         state directory       (default: $STATE_DIR)
  KUBECONFIG                              (default: $KUBECONFIG)
EOF
  exit 1
}

# ── Discovery ───────────────────────────────────────────────────

# Return the go-package path (relative to K0RDENT_APIS_DIR) for a given
# deployment name. Convention: services/<svc>/cmd/<deployment>/main.go.
# When the deployment is a nested binary (workflow-worker under services/
# workflow/cmd/workflow-worker), the walk finds it by name.
find_pkg() {
  local dep=$1
  local match
  match=$(find "$K0RDENT_APIS_DIR/services" -maxdepth 3 -type d \
    -name "$dep" -path "*/cmd/*" 2>/dev/null | head -1)
  [[ -n $match ]] || return 1
  # Return with ./ prefix so `go run` treats it as a local path.
  echo "./${match#"$K0RDENT_APIS_DIR"/}"
}

# Detect mode: "api" if a Service with the same name exists, "worker" otherwise.
detect_mode() {
  local dep=$1
  if kc get svc "$dep" >/dev/null 2>&1; then
    echo api
  else
    echo worker
  fi
}

# Node InternalIP of any Ready node; cmp01 is the only k0s node in this lab.
node_ip() {
  kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}'
}

# Pick a TCP port for the local process to bind to. Starts probing at $1
# (usually the Service's containerPort) and walks upward until it finds one
# that is neither claimed by another extract's state file nor currently
# listening on this node. Kept below the k0s NodePort range (30000+).
# Additional args ($2+) are in-flight ports from the same invocation (e.g.
# a just-picked main port when we're about to pick a metrics port) — those
# aren't in state yet, so callers pass them explicitly to avoid overlap.
pick_free_port() {
  local port=$1; shift
  local hard_stop=$((port + 1000))
  [[ $hard_stop -gt 29999 ]] && hard_stop=29999
  local used=""
  if compgen -G "$STATE_DIR"/*.json >/dev/null 2>&1; then
    used=$(jq -r '.localPort, .metricsPort | select(. != null and . != "")' \
      "$STATE_DIR"/*.json 2>/dev/null | grep -v '^$' || true)
  fi
  local extra
  for extra in "$@"; do
    used=$(printf '%s\n%s' "$used" "$extra")
  done
  while [[ $port -le $hard_stop ]]; do
    if ! grep -qx "$port" <<<"$used" \
       && ! ss -Hltn "sport = :$port" 2>/dev/null | grep -q .; then
      echo "$port"; return 0
    fi
    port=$((port + 1))
  done
  return 1
}

# ── Env capture & hosts file ────────────────────────────────────

# Kubernetes-injected env vars we don't want to leak into the local process.
# PATH/HOSTNAME are host-local; the KUBERNETES_* and *_SERVICE_* are set by
# kubelet based on in-cluster Services and don't apply on the host.
K8S_ENV_DROP_RE='^(PATH|HOSTNAME|HOME|PWD|SHLVL|TERM|_|KUBERNETES_[A-Z_]+|[A-Z0-9_]+_SERVICE_(HOST|PORT|PORT_[0-9]+_TCP(_PROTO|_PORT|_ADDR)?))='

capture_pod_env() {
  local dep=$1
  # `env` in the exec'd shell reflects kubelet's resolved container env
  # (kubelet substitutes $(VAR) references before container start).
  kc exec "deploy/$dep" -- env 2>/dev/null \
    | grep -Ev "$K8S_ENV_DROP_RE" \
    | sort
}

# Stream /etc/k0rdent-ai/ out of the running pod into $2 (a local directory
# that will be bind-mounted at runtime). Returns 0 if a tree was captured,
# 1 if the pod has no such tree (fine — env-mode services won't). Uses
# `tar` inside the container image; the k0rdent-apis images are alpine-
# based, so tar (busybox) is available.
capture_pod_files() {
  local dep=$1 out=$2
  # Existence probe: quiet if the dir isn't there.
  kc exec "deploy/$dep" -- sh -c '[ -d /etc/k0rdent-ai ]' 2>/dev/null || return 1
  mkdir -p "$out"
  # -C / + relative "etc/k0rdent-ai" preserves the leading path so `tar -xf`
  # reconstructs $out/etc/k0rdent-ai/... and the bind-mount path is stable.
  kc exec "deploy/$dep" -- tar -cf - -C / etc/k0rdent-ai 2>/dev/null \
    | tar -xpf - -C "$out"
  # Sanity check the extract landed something.
  [[ -d "$out/etc/k0rdent-ai" ]]
}

# Emit a full /etc/hosts file for the extracted process: real host content
# first, then one line per Service in $NAMESPACE and kcm-system mapping the
# ClusterIP to the four hostname shapes k8s dns exposes. Bind-mounted in a
# private mount namespace by `cmd_run`, so it never touches the real file.
build_hosts_file() {
  cat /etc/hosts
  printf '\n# ---- cluster Services (auto-generated by lab-extract.sh) ----\n'
  local ns
  for ns in "$NAMESPACE" kcm-system; do
    kubectl -n "$ns" get svc \
      -o jsonpath="{range .items[*]}{.spec.clusterIP}{'|'}{.metadata.name}{'|'}$ns{'\n'}{end}" 2>/dev/null
  done | awk -F'|' '
    $1 == "" || $1 == "None" { next }   # skip headless / unset
    { printf "%-16s %s %s.%s %s.%s.svc %s.%s.svc.cluster.local\n",
        $1, $2, $2, $3, $2, $3, $2, $3 }'
}

# ── State ───────────────────────────────────────────────────────

state_file() { echo "$STATE_DIR/$1.json"; }
env_file()   { echo "$STATE_DIR/$1.env"; }
hosts_file() { echo "$STATE_DIR/$1.hosts"; }
files_dir()  { echo "$STATE_DIR/$1.files"; }

write_state() {
  local dep=$1 mode=$2 replicas=$3 selector_json=$4 local_port=$5 pkg=$6 metrics_port=$7
  mkdir -p "$STATE_DIR"
  jq -n \
    --arg dep "$dep" \
    --arg mode "$mode" \
    --argjson replicas "$replicas" \
    --argjson selector "$selector_json" \
    --arg local_port "$local_port" \
    --arg metrics_port "$metrics_port" \
    --arg pkg "$pkg" \
    '{deployment:$dep, mode:$mode, replicas:$replicas, selector:$selector,
      localPort:$local_port, metricsPort:$metrics_port, pkg:$pkg}' \
    > "$(state_file "$dep")"
}

# ── Commands ────────────────────────────────────────────────────

cmd_start() {
  local dep=$1
  shift
  local local_port_override="" pkg_override=""
  while [[ $# -gt 0 ]]; do
    case $1 in
      --local-port) local_port_override=$2; shift 2 ;;
      --pkg)        pkg_override=$2;        shift 2 ;;
      *) die "unknown flag: $1" ;;
    esac
  done

  [[ -d $K0RDENT_APIS_DIR ]] || die "checkout not found: $K0RDENT_APIS_DIR"
  kc get "deploy/$dep" >/dev/null 2>&1 || die "no deploy/$dep in ns/$NAMESPACE"
  [[ ! -f "$(state_file "$dep")" ]] || die "$dep already extracted (see $(state_file "$dep")); run: $0 stop $dep"

  local pkg
  if [[ -n $pkg_override ]]; then
    pkg=$pkg_override
  else
    pkg=$(find_pkg "$dep") || die "no cmd/<name>/main.go for '$dep' under $K0RDENT_APIS_DIR/services — pass --pkg"
  fi
  [[ -f "$K0RDENT_APIS_DIR/${pkg#./}/main.go" ]] || die "resolved pkg '$pkg' has no main.go"

  local mode; mode=$(detect_mode "$dep")
  local replicas; replicas=$(kc get "deploy/$dep" -o jsonpath='{.spec.replicas}')
  [[ "$replicas" -gt 0 ]] || die "deploy/$dep is at 0 replicas — nothing to capture env from"

  log "extracting deploy/$dep ($mode, $replicas → 0 replicas, pkg=$pkg)"

  # Resolve local port up front so the emitted env file (SERVER_PORT override)
  # and the Endpoints hijack agree. Worker mode leaves local_port empty.
  # When --local-port is not given, probe upward from the Service's
  # containerPort until we find a port that no other extract has claimed and
  # nothing on the node is listening on — so `start` can be run repeatedly
  # for different services without any port bookkeeping by the user.
  local selector_json='{}'
  local local_port=""
  if [[ $mode == api ]]; then
    selector_json=$(kc get "svc/$dep" -o json | jq -c '.spec.selector // {}')
    local target_port; target_port=$(kc get "svc/$dep" -o jsonpath='{.spec.ports[0].targetPort}')
    if [[ -n $local_port_override ]]; then
      local_port=$local_port_override
    else
      local_port=$(pick_free_port "$target_port") \
        || die "could not find a free TCP port starting from $target_port"
      [[ "$local_port" != "$target_port" ]] \
        && ok "auto-picked local port $local_port (containerPort $target_port already in use)"
    fi
  fi

  # Capture env from the running pod. Cluster DNS names are left in the
  # values verbatim — resolution is handled by the /etc/hosts bind-mount
  # that cmd_run puts in place, so we don't have to touch the env values.
  local raw_env; raw_env=$(capture_pod_env "$dep")
  [[ -n $raw_env ]] || die "captured empty env from deploy/$dep"

  # Auto-pick a free metrics port if the pod exposes /metrics (KCS-773 opt-in;
  # chart sets METRICS_BIND_ADDR=0.0.0.0:9090). Multi-service extract needs
  # this — otherwise the second `run` fails to bind :9090.
  local metrics_port=""
  if grep -q '^METRICS_BIND_ADDR=' <<<"$raw_env"; then
    metrics_port=$(pick_free_port 9090 "$local_port") \
      || die "could not find a free metrics port"
    [[ "$metrics_port" != "9090" ]] \
      && ok "auto-picked metrics port $metrics_port (9090 already in use)"
  fi

  mkdir -p "$STATE_DIR"
  {
    echo "# lab-extract.sh — env for $dep (captured $(date -Is))"
    echo "# Cluster hostnames resolve via the per-process /etc/hosts overlay."
    printf '%s\n' "$raw_env"
    # No KUBECONFIG needed here: services that reach kube-apiserver directly
    # (workflow-worker, reconciler) will fail InClusterConfig() on the host and
    # fall back to $HOME/.kube/config — for the root user that runs this
    # script, that resolves to /root/.kube/config, the same file kubectl uses.
    if [[ -n $local_port ]]; then
      # Override SERVER_PORT so the go binary binds to the same port the
      # Endpoints object routes to. Trailing assignment wins under `set -a`.
      echo "SERVER_PORT=$local_port"
    fi
    if [[ -n $metrics_port ]]; then
      echo "METRICS_BIND_ADDR=0.0.0.0:$metrics_port"
    fi
  } > "$(env_file "$dep")"
  chmod 600 "$(env_file "$dep")"
  ok "wrote $(env_file "$dep")"

  # Snapshot the augmented /etc/hosts up front so `run` doesn't have to
  # re-query the cluster (and so the mapping is stable for the lifetime of
  # the extract even if new Services get created afterwards).
  build_hosts_file > "$(hosts_file "$dep")"
  ok "wrote $(hosts_file "$dep")"

  # Capture the pod's projected config/secret tree BEFORE scaling to 0 —
  # once the pod is gone we can't `kubectl exec` to grab its filesystem.
  # Services that use env-mode secrets have no such tree; capture_pod_files
  # returns non-zero and we skip the bind-mount at run time.
  local fd; fd=$(files_dir "$dep")
  rm -rf "$fd"
  if capture_pod_files "$dep" "$fd"; then
    ok "captured pod files → $fd/etc/k0rdent-ai"
  else
    rm -rf "$fd"
  fi

  if [[ $mode == api ]]; then
    local nip; nip=$(node_ip)
    [[ -n $nip ]] || die "could not resolve node InternalIP"

    log "hijacking svc/$dep → $nip:$local_port"
    kc patch "svc/$dep" --type=json -p='[{"op":"remove","path":"/spec/selector"}]'
    # Delete any leftover EndpointSlices auto-managed by the endpoint slice
    # controller; without this, kube-proxy still sees the old (empty) slices
    # alongside our manual Endpoints and load-balances between both.
    kc delete endpointslices -l "kubernetes.io/service-name=$dep" --ignore-not-found >/dev/null
    kc apply -f - <<EOF >/dev/null
apiVersion: v1
kind: Endpoints
metadata:
  name: $dep
  namespace: $NAMESPACE
  labels:
    lab-extract.ufo/managed: "true"
subsets:
  - addresses: [{ip: $nip}]
    ports:    [{port: $local_port}]
EOF
    ok "endpoints/$dep → $nip:$local_port"
  fi

  kc scale "deploy/$dep" --replicas=0 >/dev/null
  ok "scaled deploy/$dep → 0 (was $replicas)"

  write_state "$dep" "$mode" "$replicas" "$selector_json" "$local_port" "$pkg" "$metrics_port"

  cat >&2 <<EOF

▶ Run locally:
    sudo $0 run $dep

  (or manually, if you want to inject dlv/etc.:)
    sudo unshare -m --propagation private bash -c '
      mount --bind $(hosts_file "$dep") /etc/hosts
      [ -d $(files_dir "$dep")/etc/k0rdent-ai ] && \
        mount --bind $(files_dir "$dep")/etc/k0rdent-ai /etc/k0rdent-ai
      set -a; source $(env_file "$dep"); set +a
      cd $K0RDENT_APIS_DIR && exec go run $pkg
    '
EOF
  if [[ $mode == api ]]; then
    cat >&2 <<EOF

  In-cluster clients hit $dep.$NAMESPACE.svc → node IP $(node_ip):$local_port
  → your process. Port was auto-picked; pass --local-port N to override.
EOF
  fi
  cat >&2 <<EOF

▶ Restore:
    $0 stop $dep
EOF
}

cmd_stop() {
  local dep=$1
  local sf; sf=$(state_file "$dep")
  [[ -f $sf ]] || die "no state for $dep at $sf"

  local mode replicas selector_json
  mode=$(jq -r .mode "$sf")
  replicas=$(jq -r .replicas "$sf")
  selector_json=$(jq -c .selector "$sf")

  if [[ $mode == api ]]; then
    kc delete endpoints "$dep" --ignore-not-found >/dev/null || true
    # Restore selector (kube-controller-manager will then re-populate Endpoints
    # from the scaled-up pods).
    kc patch "svc/$dep" --type=merge -p "{\"spec\":{\"selector\":$selector_json}}" >/dev/null
    ok "restored svc/$dep selector"
  fi

  kc scale "deploy/$dep" --replicas="$replicas" >/dev/null
  ok "scaled deploy/$dep → $replicas"

  rm -f "$sf" "$(env_file "$dep")" "$(hosts_file "$dep")"
  rm -rf "$(files_dir "$dep")"
  ok "cleared state for $dep"
}

cmd_run() {
  local dep=$1
  local sf; sf=$(state_file "$dep")
  [[ -f $sf ]] || die "no state for $dep; run '$0 start $dep' first"

  local pkg ef hf fd
  pkg=$(jq -r '.pkg // ""' "$sf")
  ef=$(env_file "$dep")
  hf=$(hosts_file "$dep")
  fd=$(files_dir "$dep")
  [[ -n $pkg   ]] || die "state for $dep has no go pkg — re-run start"
  [[ -f $ef    ]] || die "env file missing: $ef"
  [[ -f $hf    ]] || die "hosts file missing: $hf"

  # Bind-mount the captured pod files at /etc/k0rdent-ai iff present. The
  # host needs a mount point to exist; create /etc/k0rdent-ai as an empty
  # directory once (cheap; visible to any user, but empty and marker-less).
  local mount_files_cmd=""
  if [[ -d "$fd/etc/k0rdent-ai" ]]; then
    mkdir -p /etc/k0rdent-ai
    mount_files_cmd="mount --bind '$fd/etc/k0rdent-ai' /etc/k0rdent-ai"
    log "→ /etc/k0rdent-ai ← $fd/etc/k0rdent-ai"
  fi

  log "→ go run $pkg for $dep (mount namespace, /etc/hosts ← $hf)"

  # unshare -m creates a private mount namespace; --propagation private
  # prevents any of our mounts from leaking back to the host. The binds
  # replace the child's view of /etc/hosts (+ optionally /etc/k0rdent-ai)
  # with our captured ones, then source the env file and exec `go run` —
  # go's default netgo resolver reads /etc/hosts directly, so cluster DNS
  # Just Works and file-projected config/secrets resolve identically.
  exec unshare -m --propagation private bash -c "
    set -e
    mount --bind '$hf' /etc/hosts
    $mount_files_cmd
    set -a
    source '$ef'
    set +a
    cd '$K0RDENT_APIS_DIR'
    exec go run '$pkg'
  "
}

cmd_status() {
  mkdir -p "$STATE_DIR"
  shopt -s nullglob
  local files=("$STATE_DIR"/*.json)
  if [[ ${#files[@]} -eq 0 ]]; then
    echo "no services extracted."
    return
  fi
  printf '%-24s %-8s %-9s %-10s %-9s %s\n' DEPLOYMENT MODE REPLICAS LOCAL_PORT METRICS ENV_FILE
  local f dep mode replicas port mport
  for f in "${files[@]}"; do
    dep=$(jq -r .deployment "$f")
    mode=$(jq -r .mode "$f")
    replicas=$(jq -r .replicas "$f")
    port=$(jq -r '.localPort // "-"' "$f")
    mport=$(jq -r '.metricsPort // "-"' "$f")
    [[ -z $port  ]] && port='-'
    [[ -z $mport ]] && mport='-'
    printf '%-24s %-8s %-9s %-10s %-9s %s\n' "$dep" "$mode" "$replicas" "$port" "$mport" "$(env_file "$dep")"
  done
}

# ── Dispatch ────────────────────────────────────────────────────

case "${1:-}" in
  start)  shift; [[ $# -ge 1 ]] || usage; cmd_start "$@" ;;
  run)    shift; [[ $# -eq 1 ]] || usage; cmd_run  "$1" ;;
  stop)   shift; [[ $# -eq 1 ]] || usage; cmd_stop "$1" ;;
  status) shift; cmd_status ;;
  *) usage ;;
esac
