#!/usr/bin/env bash
set -euo pipefail

# ─── NICo defaults ───────────────────────────────────────────────────
: "${NICO_HOST:=10.200.0.1}"
: "${NICO_KEYCLOAK_BASE_URL:=http://${NICO_HOST}:8082}"
: "${NICO_KEYCLOAK_REALM:=nico-dev}"
: "${NICO_CLIENT_ID:=nico-api}"
: "${NICO_CLIENT_SECRET:=nico-local-secret}"
: "${NICO_USERNAME:=admin@example.com}"
: "${NICO_PASSWORD:=adminpassword}"
: "${NICO_REST_API_BASE_URL:=http://${NICO_HOST}:8388}"
: "${NICO_ORG:=test-org}"

# ─── k0rdent defaults ────────────────────────────────────────────────
: "${BASE:=http://10.200.0.254:30080}"
: "${REGION:=local}"
: "${MACHINE_TYPE_NAME:=nico-lab}"
: "${MACHINE_TYPE_IMAGE_URL:=https://binary-mirantis-com.s3.amazonaws.com/openstack/bin/ubuntu/24.04/noble-server-cloudimg-amd64-20251026.img}"
: "${MACHINE_TYPE_IMAGE_CHECKSUM:=85743244cc8f2f47384480c81dbb677585d20ed693127667dbfb116f1682f793}"
: "${BMC_USERNAME:=admin}"
: "${BMC_PASSWORD:=placeholder}"

# STRICT_MATCH=true → derive capability constraints from the first NICo machine.
# Default: permissive (empty constraints match any server via the NOT-EXISTS wildcard rule).
: "${STRICT_MATCH:=false}"

# WAIT_AVAILABLE=<seconds> → after registering each server, poll until it reaches
# state=available (which means the nico-expected-machine-create workflow completed
# AND the poller populated server_inventory — required for reservation matching).
: "${WAIT_AVAILABLE:=300}"

: "${DRY_RUN:=false}"
: "${VERBOSE:=false}"

fail() { echo "ERROR: $*" >&2; exit 1; }
info() { echo "[$(date +%H:%M:%S)] $*"; }

NICO_TOKEN_URL="$NICO_KEYCLOAK_BASE_URL/realms/$NICO_KEYCLOAK_REALM/protocol/openid-connect/token"
mint_nico_token() {
  curl -sS -X POST "$NICO_TOKEN_URL" \
    -H 'Content-Type: application/x-www-form-urlencoded' \
    -d "client_id=$NICO_CLIENT_ID" \
    -d "client_secret=$NICO_CLIENT_SECRET" \
    -d "grant_type=password" \
    -d "username=$NICO_USERNAME" \
    -d "password=$NICO_PASSWORD" | jq -r '.access_token // empty'
}

# ─── Subcommand: delete-all-expected ────────────────────────────────
# Wipes every NICo ExpectedMachine in the configured org. Useful when
# iteratively resetting the NICo side while testing nico-expected-machine-create.
# NICo-only — no k0rdent token needed.
if [ "${1:-}" = "delete-all-expected" ]; then
  info "minting NICo token at $NICO_TOKEN_URL"
  NICO_TOKEN=$(mint_nico_token)
  [ -n "$NICO_TOKEN" ] || fail "NICo token mint failed"

  info "listing ExpectedMachines in org=$NICO_ORG"
  DELETED=0; FAILED=0
  while : ; do
    IDS=$(curl -sS "$NICO_REST_API_BASE_URL/v2/org/$NICO_ORG/nico/expected-machine?pageNumber=1&pageSize=100" \
      -H "Authorization: Bearer $NICO_TOKEN" | jq -r '.[].id // empty')
    [ -z "$IDS" ] && break
    PAGE_COUNT=$(echo "$IDS" | wc -l)
    info "  page of $PAGE_COUNT expected-machines"
    while read -r id; do
      [ -z "$id" ] && continue
      HTTP=$(curl -sS -o /dev/null -w '%{http_code}' -X DELETE \
        "$NICO_REST_API_BASE_URL/v2/org/$NICO_ORG/nico/expected-machine/$id" \
        -H "Authorization: Bearer $NICO_TOKEN")
      if [ "$HTTP" = "204" ] || [ "$HTTP" = "200" ]; then
        info "    ✓ $id"
        DELETED=$((DELETED+1))
      else
        info "    ✗ $id HTTP $HTTP"
        FAILED=$((FAILED+1))
      fi
    done <<< "$IDS"
    [ "$PAGE_COUNT" -lt 100 ] && break
  done
  info "done. deleted=$DELETED failed=$FAILED"
  exit 0
fi

# ─── k0rdent OAuth token flow (inlined; was external ~/login.sh) ────
: "${K0R_LOGIN_ORG:=kindmock}"
: "${K0R_LOGIN_NAMESPACE:=k0rdent-apis}"

_k0r_login() {
  local auth_ip mock_ip auth_base init auth_url trace callback token
  auth_ip=$(kubectl -n "$K0R_LOGIN_NAMESPACE" get svc auth               -o jsonpath='{.spec.clusterIP}')
  mock_ip=$(kubectl -n "$K0R_LOGIN_NAMESPACE" get svc mock-oauth2-server -o jsonpath='{.spec.clusterIP}')
  auth_base="http://$auth_ip"
  init=$(curl -sS -c /tmp/jar -X POST "$auth_base/v1/regions/global/auth/login/initiate" \
    -H 'Content-Type: application/json' \
    -d "{\"email\":\"admin@${K0R_LOGIN_ORG}.test\",\"redirectUri\":\"${BASE}/v1/regions/global/auth/callback\",\"clientId\":\"operator-portal\"}")
  auth_url=$(echo "$init" | jq -r .authorizationUrl \
    | sed "s|http://mock-oauth2-server\\.${K0R_LOGIN_NAMESPACE}\\.svc:8080|http://$mock_ip:8080|")
  trace=$(curl -sS -L --max-redirs 5 -v -b /tmp/jar -c /tmp/jar -o /dev/null "$auth_url" 2>&1 || true)
  callback=$(echo "$trace" | sed -n 's/^< [Ll]ocation: //p' | tr -d '\r' | tail -1)
  token=$(echo "$callback" | sed 's/.*[#?]access_token=//; s/&.*//')
  [ -n "$token" ] || { echo "FAIL: no token extracted. Last redirect: $callback" >&2; return 1; }
  echo "$token"
}

k0r_token() {
  local cache=/tmp/k0r-token
  umask 077
  if [ -f "$cache" ]; then
    local exp
    exp=$(cut -d. -f2 "$cache" 2>/dev/null | base64 -d 2>/dev/null | jq -r .exp 2>/dev/null)
    if [ -n "$exp" ] && [ "$exp" != "null" ] && [ $((exp - $(date +%s))) -gt 60 ]; then
      cat "$cache"
      return
    fi
  fi
  _k0r_login > "$cache" 2>/dev/null
  if ! grep -q '\..*\.' "$cache"; then
    echo "k0r_token: minting failed. Debug with: _k0r_login" >&2
    rm -f "$cache"
    return 1
  fi
  cat "$cache"
}

K0R_TOKEN=$(k0r_token)
[ -n "$K0R_TOKEN" ] || fail "k0r_token returned empty"

info "minting NICo token at $NICO_TOKEN_URL"
NICO_TOKEN=$(mint_nico_token)
[ -n "$NICO_TOKEN" ] || fail "NICo token mint failed"

# ─── Fetch NICo machines FIRST (need one to derive constraints) ─────
LIST_URL="$NICO_REST_API_BASE_URL/v2/org/$NICO_ORG/nico/machine?pageNumber=1&pageSize=100"
info "GET $LIST_URL"

RAW=$(curl -sS -w '\nHTTP %{http_code}' "$LIST_URL" -H "Authorization: Bearer $NICO_TOKEN")
STATUS=$(printf '%s' "$RAW" | tail -1 | awk '{print $2}')
BODY=$(printf '%s' "$RAW" | sed '$d')
[ "$STATUS" = "200" ] || fail "NICo list returned $STATUS: $(printf '%s' "$BODY" | head -c 400)"

if [ "$VERBOSE" = "true" ]; then
  info "  raw response top-level: $(echo "$BODY" | jq -c 'if type=="array" then "array(len=\(length))" else keys end')"
fi

COUNT=$(echo "$BODY" | jq '
  if type=="array" then length
  elif .machines then (.machines|length)
  elif .items    then (.items|length)
  elif .data     then (.data|length)
  else 0 end')

if [ "$COUNT" -eq 0 ]; then
  info "no machines returned; nothing to sync"
  exit 0
fi
info "$COUNT machine(s) available in NICo"

# Extract array location once
MACHINES=$(echo "$BODY" | jq -c 'if type=="array" then . else (.machines // .items // .data) end')

# ─── Build MachineType selector ─────────────────────────────────────
# Empty constraints → wildcard match. STRICT_MATCH derives real constraints
# from the FIRST NICo machine's MachineCapabilities so the MT only picks
# hardware-identical machines (useful for symmetric clusters).
if [ "$STRICT_MATCH" = "true" ]; then
  SEL=$(echo "$MACHINES" | jq '
    .[0].machineCapabilities as $cap
    | ($cap | map(select(.type=="CPU")) | .[0]) as $cpu
    | ($cap | map(select(.type=="GPU")) | .[0]) as $gpu
    | ($cap | map(select(.type=="Memory")) | .[0]) as $mem
    | {}
      + (if ($cpu.vendorName // "" | length) > 0
           then {cpuVendor: [$cpu.vendorName]} else {} end)
      + (if $cpu.cores then {cpuCoresMin: ($cpu.cores * ($cpu.count // 1)), cpuCoresMax: ($cpu.cores * ($cpu.count // 1))} else {} end)
      + (if $gpu then
           {}
           + (if ($gpu.vendorName // "" | length) > 0 then {gpuVendor: [$gpu.vendorName]} else {} end)
           + (if $gpu.count then {gpuInstalledMin: $gpu.count, gpuInstalledMax: $gpu.count} else {} end)
         else {} end)
  ' 2>/dev/null || echo '{}')
  info "STRICT_MATCH derived selector: $(echo "$SEL" | jq -c .)"
else
  SEL='{}'
  info "permissive selector (matches any provider=nico server with inventory)"
fi

# ─── Ensure machine_type exists ─────────────────────────────────────
info "ensuring k0rdent machine_type '$MACHINE_TYPE_NAME' in region $REGION"
if curl -sSf "$BASE/v1/regions/$REGION/infrastructure/machine-types/$MACHINE_TYPE_NAME" \
      -H "Authorization: Bearer $K0R_TOKEN" >/dev/null 2>&1; then
  info "  already exists (not updating; delete + re-run to change selector)"
else
  MT_BODY=$(jq -n \
    --arg name "$MACHINE_TYPE_NAME" \
    --arg url  "$MACHINE_TYPE_IMAGE_URL" \
    --arg sum  "$MACHINE_TYPE_IMAGE_CHECKSUM" \
    --argjson sel "$SEL" \
    '{
      name: $name,
      infrastructureProvider: "nico",
      selector: $sel,
      config: {
        image: { infrastructureProvider: "nico", url: $url, checksum: $sum },
        additionalUserData: "#cloud-config\nssh_pwauth: true\nchpasswd:\n  expire: false\n  users:\n    - {name: ubuntu, password: '"'"'qalab'"'"', type: text}\n"
      }
    }')
  if [ "$DRY_RUN" = "true" ]; then
    info "  DRY_RUN would POST machine_type: $(echo "$MT_BODY" | jq -c .)"
  else
    OUT=$(curl -sS -w '\nHTTP %{http_code}' -X POST \
      "$BASE/v1/regions/$REGION/infrastructure/machine-types" \
      -H "Authorization: Bearer $K0R_TOKEN" \
      -H 'Content-Type: application/json' \
      -d "$MT_BODY")
    CODE=$(printf '%s' "$OUT" | tail -1 | awk '{print $2}')
    MSG=$(printf '%s' "$OUT" | sed '$d')
    case "$CODE" in
      2*)  info "  created: $(echo "$MSG" | jq -r .name)" ;;
      *)   fail "machine_type create failed HTTP $CODE: $MSG" ;;
    esac
  fi
fi

# ─── Register each server ───────────────────────────────────────────
info "syncing $COUNT machine(s)"
CREATED=0; SKIPPED=0; FAILED=0
CREATED_SLUGS=()

while read -r M; do
  ID=$(echo "$M"     | jq -r .id)
  STATE=$(echo "$M"  | jq -r .status)
  SERIAL=$(echo "$M" | jq -r '.serialNumber // .controllerMachineId // .id')
  BMC_MAC=$(echo "$M" | jq -r '
    (.machineInterfaces[]? | select((.role//"") == "bmc") | .macAddress)
    // .machineInterfaces[0].macAddress // ""')
  if [ -z "$BMC_MAC" ] || [ "$BMC_MAC" = "null" ]; then
    BMC_MAC=$(printf '02:%s' "$(echo -n "$ID" | md5sum | head -c 10 | sed 's/../&:/g;s/:$//')")
  fi
  DISP=$(echo "$M" | jq -r '.productName // .id')

  SRV_BODY=$(jq -n \
    --arg display "$DISP" --arg mac "$BMC_MAC" --arg serial "$SERIAL" \
    --arg user "$BMC_USERNAME" --arg pw "$BMC_PASSWORD" \
    '{
      infrastructureProvider: "nico",
      displayName: $display,
      serverCreateParameters: {
        chassisSerialNumber: $serial,
        bmc: {macAddress: $mac, username: $user, password: $pw}
      },
      targetState: "available"
    }')

  if [ "$DRY_RUN" = "true" ]; then
    info "  DRY_RUN id=$ID mac=$BMC_MAC serial=$SERIAL state=$STATE"
    SKIPPED=$((SKIPPED+1)); continue
  fi

  OUT=$(curl -sS -w '\nHTTP %{http_code}' -X POST \
    "$BASE/v1/regions/$REGION/infrastructure/servers" \
    -H "Authorization: Bearer $K0R_TOKEN" \
    -H 'Content-Type: application/json' -d "$SRV_BODY")
  CODE=$(printf '%s' "$OUT" | tail -1 | awk '{print $2}')
  BODY_LINE=$(printf '%s' "$OUT" | sed '$d')
  MSG=$(printf '%s' "$BODY_LINE" | jq -c '.error // .server // .' 2>/dev/null || echo "$BODY_LINE")
  case "$CODE" in
    2*)
      SLUG=$(printf '%s' "$BODY_LINE" | jq -r .id)
      info "  ✓ $ID → k0rdent slug=$SLUG (mac=$BMC_MAC)"
      CREATED=$((CREATED+1))
      CREATED_SLUGS+=("$SLUG")
      ;;
    409) info "  = $ID already registered"; SKIPPED=$((SKIPPED+1)) ;;
    *)   info "  ✗ $ID HTTP $CODE $MSG"; FAILED=$((FAILED+1)) ;;
  esac
done < <(echo "$MACHINES" | jq -c '.[]')

info "sync done. created=$CREATED skipped=$SKIPPED failed=$FAILED"

# ─── Wait for each new server to reach state=available ──────────────
# Reservation matching requires:
#   1. The MT exists (done above)
#   2. The server has an inventory row (populated by the poller after
#      nico-expected-machine-create completes and returns machine data)
#   3. The server's current_state is in {registered, available}
# When state transitions to `available`, both (2) and (3) are satisfied.
if [ "$WAIT_AVAILABLE" != "0" ] && [ "${#CREATED_SLUGS[@]}" -gt 0 ]; then
  info "waiting up to ${WAIT_AVAILABLE}s for ${#CREATED_SLUGS[@]} server(s) to reach state=available"
  DEADLINE=$(( $(date +%s) + WAIT_AVAILABLE ))
  READY=0
  while [ "$(date +%s)" -lt "$DEADLINE" ]; do
    READY=0
    for slug in "${CREATED_SLUGS[@]}"; do
      st=$(curl -sS "$BASE/v1/regions/$REGION/infrastructure/servers/$slug" \
             -H "Authorization: Bearer $K0R_TOKEN" \
           | jq -r '.currentState // "?"')
      [ "$st" = "available" ] && READY=$((READY+1))
    done
    info "  $READY/${#CREATED_SLUGS[@]} available"
    [ "$READY" -eq "${#CREATED_SLUGS[@]}" ] && break
    sleep 5
  done
  if [ "$READY" -ne "${#CREATED_SLUGS[@]}" ]; then
    info "FAIL: $((${#CREATED_SLUGS[@]} - READY)) server(s) did not reach state=available within ${WAIT_AVAILABLE}s"
    for slug in "${CREATED_SLUGS[@]}"; do
      st=$(curl -sS "$BASE/v1/regions/$REGION/infrastructure/servers/$slug" \
             -H "Authorization: Bearer $K0R_TOKEN" \
           | jq -r '.currentState + " (transition=" + (.transitionState // "-") + ")"')
      info "    $slug: $st"
    done
    info "check workflow-worker logs: kubectl -n k0rdent-apis logs deploy/workflow-worker | grep expected-machine"
    exit 1
  else
    info "all newly-created servers reached state=available — reservation matching should work"
  fi
fi

# ─── Show a quick reservation-readiness check ───────────────────────
info "reservation readiness check for machineType='$MACHINE_TYPE_NAME':"
info "  try: k0r-internal reserve $MACHINE_TYPE_NAME 1     # via kong-internal listener"
info "  or:  bash nico-inventory.sh                        # visualise the full inventory"
