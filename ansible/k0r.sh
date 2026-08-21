#!/usr/bin/env bash
# k0r.sh — cross-reference k0rdent servers ↔ NICo machines/expected/instances.
#
# Usage:
#   k0r.sh            # summary tables only
#   k0r.sh --full     # also dump per-record JSON for instance-types + instances
#
# Can be executed as a script, or sourced from bash to expose the helper
# functions (k0r_login, k0r_token, k0r, nico_token, nico_get) for reuse:
#   source k0r.sh
#   k0r /v1/regions/local/infrastructure/servers | jq
#
# Config (env vars, defaults shown in parentheses):
#   BASE                     k0rdent Kong external listener   (http://10.200.0.254:30080)
#   REGION                   k0rdent region                   (local)
#   K0R_TOKEN_FILE           cache for the k0rdent JWT        (/tmp/k0r-token)
#   TOKEN_SLACK_SEC          re-mint threshold, seconds       (60)
#   K0R_NAMESPACE            namespace with auth + mock-oauth2-server (k0rdent-apis)
#   K0R_LOGIN_EMAIL          login email for k0r_login        (admin@kind.test)
#   K0R_LOGIN_CLIENT_ID      OAuth client id                  (operator-portal)
#   K0R_LOGIN_REDIRECT_URI   OAuth redirect URI               ($BASE/v1/regions/global/auth/callback)
#   NICO_HOST, NICO_KEYCLOAK_*, NICO_REST_API_BASE_URL, NICO_ORG
#                            NICo access (defaults to the lab mock at 10.200.0.1)

# ── Config ──────────────────────────────────────────────────────
: "${BASE:=http://10.200.0.254:30080}"
: "${REGION:=local}"

: "${K0R_TOKEN_FILE:=/tmp/k0r-token}"
: "${TOKEN_SLACK_SEC:=60}"

: "${K0R_NAMESPACE:=k0rdent-apis}"
: "${K0R_LOGIN_EMAIL:=admin@kind.test}"
: "${K0R_LOGIN_CLIENT_ID:=operator-portal}"
: "${K0R_LOGIN_REDIRECT_URI:=${BASE}/v1/regions/global/auth/callback}"

: "${NICO_HOST:=10.200.0.1}"
: "${NICO_KEYCLOAK_BASE_URL:=http://${NICO_HOST}:8082}"
: "${NICO_KEYCLOAK_REALM:=nico-dev}"
: "${NICO_CLIENT_ID:=nico-api}"
: "${NICO_CLIENT_SECRET:=nico-local-secret}"
: "${NICO_USERNAME:=admin@example.com}"
: "${NICO_PASSWORD:=adminpassword}"
: "${NICO_REST_API_BASE_URL:=http://${NICO_HOST}:8388}"
: "${NICO_ORG:=test-org}"

# ── Auth + fetch helpers (always defined so they work when sourced) ──

# k0r_login — mint a fresh k0rdent operator JWT via the mock-oauth2 login flow.
# Resolves the in-cluster ClusterIPs for the `auth` and `mock-oauth2-server` svcs
# in $K0R_NAMESPACE, drives the /auth/login/initiate → /authorize redirect chain,
# and prints the extracted access token on stdout. Non-zero on any failure.
k0r_login() {
  local ns=${K0R_NAMESPACE:-k0rdent-apis}
  local jar auth_ip mock_ip auth_base init body auth_url trace callback token

  jar=$(mktemp -t k0r-jar.XXXXXX) || return 1

  auth_ip=$(kubectl -n "$ns" get svc auth               -o jsonpath='{.spec.clusterIP}' 2>/dev/null)
  mock_ip=$(kubectl -n "$ns" get svc mock-oauth2-server -o jsonpath='{.spec.clusterIP}' 2>/dev/null)
  if [ -z "$auth_ip" ] || [ -z "$mock_ip" ]; then
    rm -f "$jar"
    echo "k0r_login: could not resolve cluster IPs (auth=$auth_ip mock=$mock_ip)" >&2
    return 1
  fi
  auth_base="http://$auth_ip"

  body=$(jq -n \
    --arg e "$K0R_LOGIN_EMAIL" \
    --arg r "$K0R_LOGIN_REDIRECT_URI" \
    --arg c "$K0R_LOGIN_CLIENT_ID" \
    '{email:$e, redirectUri:$r, clientId:$c}')

  init=$(curl -sS -c "$jar" -X POST "$auth_base/v1/regions/global/auth/login/initiate" \
    -H 'Content-Type: application/json' -d "$body")

  auth_url=$(echo "$init" | jq -r '.authorizationUrl // empty' \
    | sed "s|http://mock-oauth2-server\.${ns}\.svc:8080|http://$mock_ip:8080|")
  if [ -z "$auth_url" ]; then
    rm -f "$jar"
    echo "k0r_login: no authorizationUrl in init response: $init" >&2
    return 1
  fi

  # Follow redirects; grab the last Location header (contains #access_token=...).
  trace=$(curl -sS -L --max-redirs 5 -v -b "$jar" -c "$jar" -o /dev/null "$auth_url" 2>&1 || true)
  callback=$(echo "$trace" | sed -n 's/^< [Ll]ocation: //p' | tr -d '\r' | tail -1)
  token=$(echo "$callback" | sed 's/.*[#?]access_token=//; s/&.*//')
  rm -f "$jar"

  if [ -z "$token" ]; then
    echo "k0r_login: no token extracted. Last redirect: $callback" >&2
    return 1
  fi
  printf '%s\n' "$token"
}

# k0rdent operator token — cached in $K0R_TOKEN_FILE (0600), refreshed via k0r_login
# when the cache is missing or the JWT is within $TOKEN_SLACK_SEC of expiry.
k0r_token() {
  local cache=${K0R_TOKEN_FILE:-/tmp/k0r-token}
  local slack=${TOKEN_SLACK_SEC:-60}
  umask 077

  if [ -f "$cache" ]; then
    local exp
    exp=$(cut -d. -f2 "$cache" 2>/dev/null | base64 -d 2>/dev/null | jq -r '.exp // empty' 2>/dev/null)
    if [ -n "$exp" ] && [ $((exp - $(date +%s))) -gt "$slack" ]; then
      cat "$cache"
      return 0
    fi
  fi

  local tok
  tok=$(k0r_login) || { echo "k0r_token: minting failed" >&2; return 1; }
  if ! echo "$tok" | grep -q '\..*\.'; then
    echo "k0r_token: k0r_login did not return a JWT" >&2
    return 1
  fi
  printf '%s\n' "$tok" > "$cache"
  cat "$cache"
}

# Convenience wrapper: k0r <path> [curl args...]
k0r() {
  local path=$1; shift
  curl -sS "${BASE}${path}" -H "Authorization: Bearer $(k0r_token)" "$@"
}

# NICo password-grant token; echoes JWT on stdout, non-zero on failure.
nico_token() {
  local tok
  tok=$(curl -sS -X POST \
    "${NICO_KEYCLOAK_BASE_URL}/realms/${NICO_KEYCLOAK_REALM}/protocol/openid-connect/token" \
    -H 'Content-Type: application/x-www-form-urlencoded' \
    -d "client_id=${NICO_CLIENT_ID}" \
    -d "client_secret=${NICO_CLIENT_SECRET}" \
    -d "grant_type=password" \
    -d "username=${NICO_USERNAME}" \
    -d "password=${NICO_PASSWORD}" | jq -r '.access_token // empty')
  if [ -z "$tok" ]; then
    echo "nico_token: mint failed at ${NICO_KEYCLOAK_BASE_URL}" >&2
    return 1
  fi
  printf '%s\n' "$tok"
}

# NICo GET — echoes JSON array; returns [] on any error.
# Reuses $NICO_TOKEN if already set (fast-path when called many times).
nico_get() {
  local path=$1
  local tok=${NICO_TOKEN:-}
  if [ -z "$tok" ]; then
    tok=$(nico_token) || { echo '[]'; return 0; }
  fi
  local resp code body
  resp=$(curl -sS -w '\nHTTP %{http_code}' \
    "${NICO_REST_API_BASE_URL}${path}" \
    -H "Authorization: Bearer $tok")
  code=$(printf '%s' "$resp" | tail -1 | awk '{print $2}')
  body=$(printf '%s' "$resp" | sed '$d')
  if [ "$code" = "200" ] && echo "$body" | jq -e 'type == "array"' >/dev/null 2>&1; then
    echo "$body"
  else
    echo "warn: NICo GET $path returned $code" >&2
    echo '[]'
  fi
}

# Exports so `bash nico-sync.sh` and other subshells inherit the functions.
export -f k0r_login k0r_token k0r nico_token nico_get

# ── Main (only when executed, not sourced) ─────────────────────
_nico_inventory_main() {
  # Args: --full shows per-record JSON for instance-types and instances.
  local FULL=0
  local arg
  for arg in "$@"; do
    case "$arg" in
      --full|-f) FULL=1 ;;
      -h|--help)
        echo "Usage: $0 [--full]"
        echo "  --full   show per-record JSON for NICo instance-types and instances"
        return 0
        ;;
      *) echo "unknown arg: $arg (use --full or --help)" >&2; return 2 ;;
    esac
  done

  # Cache NICo token for the rest of the run so nico_get doesn't re-mint.
  NICO_TOKEN=$(nico_token) || { echo "ERROR: NICo token mint failed" >&2; return 1; }
  export NICO_TOKEN

  local NICO_MACHINES NICO_EXPECTED NICO_SITES NICO_INSTANCE_TYPES NICO_INSTANCES
  NICO_MACHINES=$(nico_get       "/v2/org/$NICO_ORG/nico/machine?pageNumber=1&pageSize=100")
  NICO_EXPECTED=$(nico_get       "/v2/org/$NICO_ORG/nico/expected-machine?pageNumber=1&pageSize=100")
  NICO_SITES=$(nico_get          "/v2/org/$NICO_ORG/nico/site?pageNumber=1&pageSize=100")
  NICO_INSTANCE_TYPES=$(nico_get "/v2/org/$NICO_ORG/nico/instance/type?pageNumber=1&pageSize=100")
  NICO_INSTANCES=$(nico_get      "/v2/org/$NICO_ORG/nico/instance?pageNumber=1&pageSize=100")

  # Per-instance-type associated machines: { itid: [machineId, ...] }
  local NICO_IT_MACHINES='{}'
  local itid assocs
  while IFS= read -r itid; do
    [ -z "$itid" ] && continue
    assocs=$(nico_get "/v2/org/$NICO_ORG/nico/instance/type/$itid/machine?pageNumber=1&pageSize=100")
    NICO_IT_MACHINES=$(jq -n \
      --argjson m "$NICO_IT_MACHINES" \
      --arg k "$itid" \
      --argjson v "$assocs" \
      '$m + {($k): ($v | map(.machineId) | map(select(. != null)))}')
  done < <(echo "$NICO_INSTANCE_TYPES" | jq -r '.[].id // empty')

  # ── k0rdent state (public API via the k0r wrapper) ───────────
  local K0R_MTS K0R_SERVERS
  K0R_MTS=$(k0r "/v1/regions/$REGION/infrastructure/machine-types")

  K0R_SERVERS=$(
    k0r "/v1/regions/$REGION/infrastructure/servers?pageSize=200" \
      | jq -r '.servers[]?.id // empty' \
      | while read -r slug; do
          [ -z "$slug" ] && continue
          srv=$(k0r "/v1/regions/$REGION/infrastructure/servers/$slug")
          psd=$(k0r "/v1/regions/$REGION/infrastructure/servers/$slug/providerSpecificData" 2>/dev/null || echo null)
          jq -n --argjson s "$srv" --argjson p "$psd" \
            '$s + {providerSpecificData: (if ($p | type) == "object" then $p else {} end)}'
        done | jq -s .
  )

  # ── Build the aggregate matrix ───────────────────────────────
  local MATRIX
  MATRIX=$(jq -n \
    --argjson nm  "$NICO_MACHINES" \
    --argjson ne  "$NICO_EXPECTED" \
    --argjson ns  "$NICO_SITES" \
    --argjson nit "$NICO_INSTANCE_TYPES" \
    --argjson ni  "$NICO_INSTANCES" \
    --argjson itm "$NICO_IT_MACHINES" \
    --argjson kmt "$K0R_MTS" \
    --argjson k   "$K0R_SERVERS" '
    def cap($t): (.machineCapabilities // []) | map(select(.type == $t)) | .[0];
    def fmt_cpu:
      cap("CPU") as $c
      | if $c == null then "-"
        else "\($c.count // 1)x \($c.cores // "?")c/\($c.threads // "?")t \(($c.name // $c.vendorName) // "CPU")"
        end;
    def fmt_mem:
      cap("Memory") as $m
      | if $m == null then "-"
        else "\($m.count // 1)x \($m.capacity // "?")" end;
    def fmt_gpu:
      cap("GPU") as $g
      | if $g == null then "-"
        else
          (($g.vendorName // $g.family // "GPU") as $lbl
           | if ($g.family != null and $g.family != $lbl)
             then "\($g.count // 1)x \($lbl) \($g.family)"
             else "\($g.count // 1)x \($lbl)" end)
        end;
    def primary_iface:
      ((.machineInterfaces // []) as $ifs
       | ($ifs | map(select(.isPrimary == true or .primary_interface == true)) | .[0]) // ($ifs[0] // {}));
    def machine_name: primary_iface.hostname // .serialNumber // .id;

    ($ns  | map({key: .id, value: .name}) | from_entries) as $siteName |
    ($nm  | map({key: .id, value: .}) | from_entries) as $machineById |
    ($ni  | map(select(.machineId != null) | {key: .machineId, value: .}) | from_entries) as $instanceByMachine |
    ($nm  | map({key: .id, value: machine_name}) | from_entries) as $machineNameById |
    ($nit | map({key: (.id // ""), value: (.name // .id)}) | from_entries) as $itNameById |
    ($kmt.machineTypes // []) as $k0rMTs |
    {
      summary: {
        k0rdent_servers: ($k | length),
        k0rdent_machine_types: ($k0rMTs | length),
        nico_machines: ($machineById | length),
        nico_expected: ($ne | length),
        nico_instances: ($ni | length),
        nico_instance_types: ($nit | length),
        nico_sites: ($siteName | length)
      },
      k0rdent_machine_types: [$k0rMTs[] | {
        name,
        provider: .infrastructureProvider,
        image: ((.config.image.url) // "-"),
        selector: (.selector // {})
      }],
      nico_expected_machines: [$ne[] | {
        id,
        chassis: (.chassisSerialNumber // "-"),
        bmc: (.bmc.macAddress // "-"),
        site: ($siteName[.siteId // ""] // (.siteId // "-")),
        linkedMachineId: (.linkedMachineId // .machineId // "-")
      }],
      nico_machines: [$nm[] | {
        id,
        status: (.status // "-"),
        site: ($siteName[.siteId // ""] // (.siteId // "-")),
        vendor: (.vendor // "-"),
        product: (.productName // "-"),
        serial: (.serialNumber // "-"),
        cpu: fmt_cpu,
        memory: fmt_mem,
        gpu: fmt_gpu,
        hostname: (primary_iface.hostname // "-"),
        primaryMac: (primary_iface.macAddress // "-"),
        instanceId: (.instanceId // "-"),
        tenantId: (.tenantId // "-"),
        usable: (.isUsableByTenant // false)
      }],
      nico_instance_type_details: [$nit[] | {
        id: (.id // "-"),
        record: .,
        siteName: ($siteName[.siteId // ""] // (.siteId // "-")),
        associations: (($itm[(.id // "")] // []) | map({id: ., name: ($machineNameById[.] // "?")}))
      }],
      nico_instances: [$ni[] | {
        id: (.id // "-"),
        name: (.name // "-"),
        status: (.status // "-"),
        machineId: (.machineId // "-"),
        instanceTypeId: (.instanceTypeId // "-"),
        operatingSystemId: (.operatingSystemId // "-"),
        siteName: ($siteName[.siteId // ""] // (.siteId // "-"))
      }],
      nico_instance_details: [$ni[] | {
        id: (.id // "-"),
        record: .,
        siteName: ($siteName[.siteId // ""] // (.siteId // "-")),
        machineName: (if ((.machineId // "") == "") then "-" else ($machineNameById[.machineId] // "?") end),
        instanceTypeName: (if ((.instanceTypeId // "") == "") then "-" else ($itNameById[.instanceTypeId] // "?") end)
      }],
      rows: [
        $k[] | . as $srv
        | ($srv.providerSpecificData // {}) as $psd
        | ($machineById[$psd.machine_id // ""] // null) as $mach
        | ($instanceByMachine[$psd.machine_id // ""] // null) as $inst
        | {
            k0rSlug:      $srv.id,
            state:        ($srv.currentState // "-"),
            bmcMac:       ($srv.bmcMacAddress // "-"),
            chassis:      ($srv.chassisSerialNumber // "-"),
            machineId:    ($psd.machine_id // "-"),
            site:         ($siteName[$psd.site_id // ""] // ($psd.site_id // "-")),
            nicoStatus:   ($mach.status // (if $mach == null then "ORPHAN" else "-" end)),
            instanceId:   ($inst.id // "-"),
            instanceStatus: ($inst.status // "-")
          }
      ]
    }')

  # ── Render (matches nico-inventory.py sections + order) ──────
  echo "$MATRIX" | jq -r '
    "SUMMARY: k0rdent[servers=\(.summary.k0rdent_servers) mt=\(.summary.k0rdent_machine_types)] | nico[machines=\(.summary.nico_machines) expected=\(.summary.nico_expected) instances=\(.summary.nico_instances) instTypes=\(.summary.nico_instance_types) sites=\(.summary.nico_sites)]"
  '

  echo ""
  echo "=== k0rdent machine-types ==="
  echo "$MATRIX" | jq -r '
    if (.k0rdent_machine_types | length) == 0 then "  (none)"
    else
      (["NAME","PROVIDER","IMAGE","SELECTOR"] | @tsv),
      (.k0rdent_machine_types[] | [.name, .provider, .image, (.selector | tostring)] | @tsv)
    end
  ' | column -t -s $'\t'

  echo ""
  echo "=== NICo expected-machines ==="
  echo "$MATRIX" | jq -r '
    if (.nico_expected_machines | length) == 0 then "  (none)"
    else
      (["ID","CHASSIS","BMC_MAC","SITE","LINKED_MACHINE"] | @tsv),
      (.nico_expected_machines[] | [.id, .chassis, .bmc, .site, .linkedMachineId] | @tsv)
    end
  ' | column -t -s $'\t'

  echo ""
  echo "=== NICo machines ==="
  echo "$MATRIX" | jq -r '
    if (.nico_machines | length) == 0 then "  (none)"
    else
      (["ID","STATUS","SITE","SERIAL","GPU","HOSTNAME","PRIMARY_MAC","INSTANCE","TENANT"] | @tsv),
      (.nico_machines[] | [
        .id, .status, .site, .serial,
        .gpu, .hostname, .primaryMac, .instanceId, .tenantId
      ] | @tsv)
    end
  ' | column -t -s $'\t'

  echo ""
  echo "=== NICo instance-types ==="
  echo "$MATRIX" | jq -r '
    if (.nico_instance_type_details | length) == 0 then "  (none)"
    else
      (["ID","NAME","SITE","STATUS","MACHINES"] | @tsv),
      (.nico_instance_type_details[] | [
        .id, (.record.name // "-"), .siteName, (.record.status // "-"),
        (.associations | length | tostring)
      ] | @tsv)
    end
  ' | column -t -s $'\t'

  if [ "$FULL" = "1" ]; then
    echo ""
    echo "=== NICo instance-types (full detail) ==="
    if [ "$(echo "$MATRIX" | jq '.nico_instance_type_details | length')" = "0" ]; then
      echo "  (none)"
    else
      echo "$MATRIX" | jq -c '.nico_instance_type_details[]' | while IFS= read -r it; do
        itid=$(echo "$it" | jq -r '.id')
        site=$(echo "$it" | jq -r '.siteName')
        echo "---- $itid ----"
        echo "$it" | jq --sort-keys '.record'
        echo "  siteName:           $site"
        echo "  associatedMachines: $(echo "$it" | jq '.associations | length')"
        echo "$it" | jq -r '
          if (.associations | length) == 0 then "    - (none)"
          else (.associations[] | "    - \(.id) (\(.name))")
          end'
        echo ""
      done
    fi
  fi

  echo ""
  echo "=== NICo instances ==="
  echo "$MATRIX" | jq -r '
    if (.nico_instances | length) == 0 then "  (none)"
    else
      (["ID","NAME","STATUS","MACHINE","INST_TYPE","OS","SITE"] | @tsv),
      (.nico_instances[] | [
        .id, .name, .status, .machineId, .instanceTypeId, .operatingSystemId, .siteName
      ] | @tsv)
    end
  ' | column -t -s $'\t'

  if [ "$FULL" = "1" ]; then
    echo ""
    echo "=== NICo instances (full detail) ==="
    if [ "$(echo "$MATRIX" | jq '.nico_instance_details | length')" = "0" ]; then
      echo "  (none)"
    else
      echo "$MATRIX" | jq -c '.nico_instance_details[]' | while IFS= read -r i; do
        iid=$(echo "$i"  | jq -r '.id')
        site=$(echo "$i" | jq -r '.siteName')
        mach=$(echo "$i" | jq -r '.machineName')
        it=$(echo "$i"   | jq -r '.instanceTypeName')
        echo "---- $iid ----"
        echo "$i" | jq --sort-keys '.record'
        echo "  siteName:        $site"
        echo "  machineName:     $mach"
        echo "  instanceTypeName:$it"
        echo ""
      done
    fi
  fi

  echo ""
  echo "=== Servers (k0rdent ↔ NICo) ==="
  echo "$MATRIX" | jq -r '
    (["K0R_SLUG","STATE","BMC_MAC","CHASSIS","NICO_MACHINE","MSTATUS","SITE","INSTANCE","ISTATUS"] | @tsv),
    (.rows[] | [
      .k0rSlug, .state, .bmcMac, .chassis, .machineId,
      .nicoStatus, .site, .instanceId, .instanceStatus
    ] | @tsv)
  ' | column -t -s $'\t'

  # ── Drift ────────────────────────────────────────────────────
  local ORPHANS COUNT
  ORPHANS=$(echo "$MATRIX" | jq '[.rows[] | select(.nicoStatus == "ORPHAN")]')
  COUNT=$(echo "$ORPHANS" | jq 'length')
  if [ "$COUNT" -gt 0 ]; then
    echo ""
    echo "⚠ $COUNT k0rdent rows point at a NICo machine that no longer exists:"
    echo "$ORPHANS" | jq -r '.[] | "  \(.k0rSlug) → machineId=\(.machineId)"'
  fi

  local DANGLING DCOUNT
  DANGLING=$(echo "$MATRIX" | jq '
    (.rows | map(.machineId) | map(select(. != "-"))) as $known
    | [.nico_machines[] | select(.id as $id | ($known | index($id) | not))]')
  DCOUNT=$(echo "$DANGLING" | jq 'length')
  if [ "$DCOUNT" -gt 0 ]; then
    echo ""
    echo "⚠ $DCOUNT NICo machines are NOT registered in k0rdent:"
    echo "$DANGLING" | jq -r '.[] | "  \(.id)  site=\(.site)  status=\(.status)"'
  fi

  local STALE SCOUNT
  STALE=$(echo "$MATRIX" | jq '
    (.rows | map(.machineId) | map(select(. != "-"))) as $known
    | [.nico_instances[] | select(.machineId | (. == "-" or ($known | index(.) | not)))]')
  SCOUNT=$(echo "$STALE" | jq 'length')
  if [ "$SCOUNT" -gt 0 ]; then
    echo ""
    echo "⚠ $SCOUNT NICo instance(s) not tied to any k0rdent server:"
    echo "$STALE" | jq -r '.[] | "  \(.id)  machineId=\(.machineId)  status=\(.status)"'
  fi
}

# Only run main when executed as a script; when sourced, just expose the helpers.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  set -euo pipefail
  [ "${DEBUG:-}" = "1" ] && set -x
  _nico_inventory_main "$@"
fi
