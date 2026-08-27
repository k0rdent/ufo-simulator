#!/usr/bin/env bash
# Minimal REST client for the k0rdent compute/infrastructure/organizations API.
# Region-global resources follow /v1/regions/global/<service>/<resource-type>[/<id>]
# (e.g. compute/cluster-types, infrastructure/machine-types). Project-scoped
# resources (e.g. compute/clusters, compute/instances, compute/instance-groups)
# instead follow /v1/regions/global/projects/<project>/<service>/<resource-type>[/<id>] --
# set PROJECT to switch a call over to that shape.
#
# Usage:
#   export API_BASE=https://api.global.gai.inference.miralabs.dev
#   export TOKEN=$(scenarios/auth/get-token.sh)
#   export PROJECT=<project-id>   # only needed for project-scoped resources
#
#   k0r.sh list   <resource-path>
#   k0r.sh get    <resource-path> <id>
#   k0r.sh get    <resource-path> --file <body.json|body.yaml>
#   k0r.sh create <resource-path> --file <body.json|body.yaml>
#   k0r.sh update <resource-path> <id> --file <body.json|body.yaml>
#   k0r.sh apply  <resource-path> --file <body.json|body.yaml>
#   k0r.sh delete <resource-path> <id>
#   k0r.sh delete <resource-path> --file <body.json|body.yaml>
#
# apply reads id from the file, GETs the object, and either creates (404) or
# PATCH-updates (200) with version taken from the existing API object.
#
# --file accepts either JSON or YAML (by extension: .yaml/.yml is converted
# to JSON via python3+pyyaml before sending; anything else is sent as-is).
#
# Examples:
#   k0r.sh list compute/cluster-types                       # region-global
#   k0r.sh get compute/cluster-types standalone-cp-gpu-ib-nvlink
#   k0r.sh get compute/cluster-types --file body.yaml
#   k0r.sh create compute/cluster-types --file body.json
#   k0r.sh create compute/cluster-types --file body.yaml
#   k0r.sh update compute/cluster-types standalone-cp-gpu-ib-nvlink --file patch.json
#   k0r.sh apply compute/cluster-types --file body.yaml
#   k0r.sh delete compute/cluster-types standalone-cp-gpu-ib-nvlink
#   k0r.sh delete compute/cluster-types --file body.yaml
#   PROJECT=demo k0r.sh list compute/clusters               # project-scoped
#   PROJECT=demo k0r.sh get compute/clusters lab-cluster-1
#   PROJECT=demo k0r.sh update compute/clusters lab-cluster-1 --file patch.yaml
#   PROJECT=demo k0r.sh apply compute/clusters --file cluster.yaml

set -euo pipefail

: "${API_BASE:?set API_BASE}"
: "${TOKEN:?set TOKEN}"
PROJECT="${PROJECT:-}"

usage() {
  echo "usage: $0 list <resource-path>" >&2
  echo "       $0 get <resource-path> <id>" >&2
  echo "       $0 get <resource-path> --file <body.json|body.yaml>" >&2
  echo "       $0 create <resource-path> --file <body.json|body.yaml>" >&2
  echo "       $0 update <resource-path> <id> --file <body.json|body.yaml>" >&2
  echo "       $0 apply <resource-path> --file <body.json|body.yaml>" >&2
  echo "       $0 delete <resource-path> <id>" >&2
  echo "       $0 delete <resource-path> --file <body.json|body.yaml>" >&2
  echo "       (set PROJECT for project-scoped resources, e.g. compute/clusters)" >&2
  exit 1
}

load_body() {
  local file=$1
  [ -f "$file" ] || { echo "k0r.sh: file not found: $file" >&2; exit 1; }
  case "$file" in
    *.yaml|*.yml)
      python3 -c 'import sys, json, yaml; json.dump(yaml.safe_load(open(sys.argv[1])), sys.stdout)' "$file"
      ;;
    *)
      cat "$file"
      ;;
  esac
}

id_from_file() {
  local file=$1 body id
  body=$(load_body "$file")
  id=$(echo "$body" | jq -r '.id // empty')
  [ -n "$id" ] || { echo "k0r.sh: file must contain an id field" >&2; exit 1; }
  printf '%s\n' "$id"
}

api_request() {
  local method=$1 url=$2 body=${3:-}
  if [ -n "$body" ]; then
    curl -sS -X "$method" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
      -d "$body" "$url"
  else
    curl -sS -X "$method" -H "Authorization: Bearer $TOKEN" "$url"
  fi
}

VERB="${1:-}"
RESOURCE="${2:-}"
[ -n "$VERB" ] && [ -n "$RESOURCE" ] || usage

URL="${API_BASE}/v1/regions/local/${PROJECT:+projects/$PROJECT/}${RESOURCE}"

case "$VERB" in
  list)
    echo curl -s -H "Authorization: Bearer $TOKEN" "$URL"
    curl -s -H "Authorization: Bearer $TOKEN" "$URL" | jq
    ;;
  get)
    if [ "${3:-}" = "--file" ]; then
      FILE="${4:?usage: $0 get <resource-path> --file <body.json|body.yaml>}"
      ID=$(id_from_file "$FILE")
    else
      ID="${3:?usage: $0 get <resource-path> <id> | $0 get <resource-path> --file <body.json|body.yaml>}"
    fi
    curl -s -H "Authorization: Bearer $TOKEN" "$URL/$ID" | jq
    ;;
  create)
    [ "${3:-}" = "--file" ] || usage
    FILE="${4:?usage: $0 create <resource-path> --file <body.json|body.yaml>}"
    BODY=$(load_body "$FILE")
    api_request POST "$URL" "$BODY" | jq
    ;;
  update)
    ID="${3:?usage: $0 update <resource-path> <id> --file <body.json|body.yaml>}"
    [ "${4:-}" = "--file" ] || usage
    FILE="${5:?usage: $0 update <resource-path> <id> --file <body.json|body.yaml>}"
    BODY=$(load_body "$FILE")
    api_request PATCH "$URL/$ID" "$BODY" | jq
    ;;
  apply)
    [ "${3:-}" = "--file" ] || usage
    FILE="${4:?usage: $0 apply <resource-path> --file <body.json|body.yaml>}"
    BODY=$(load_body "$FILE")
    ID=$(id_from_file "$FILE")

    GET_RESP=$(mktemp)
    trap 'rm -f "$GET_RESP"' EXIT
    HTTP_CODE=$(curl -sS -o "$GET_RESP" -w '%{http_code}' \
      -H "Authorization: Bearer $TOKEN" "$URL/$ID")

    case "$HTTP_CODE" in
      404)
        echo "k0r.sh: $ID not found, creating" >&2
        api_request POST "$URL" "$BODY" | jq
        ;;
      200)
        if ! jq -e 'has("version")' "$GET_RESP" >/dev/null; then
          echo "k0r.sh: existing object $ID has no version field" >&2
          exit 1
        fi
        BODY=$(echo "$BODY" | jq --slurpfile existing "$GET_RESP" 'del(.id) + {version: $existing[0].version}')
        echo "k0r.sh: $ID found, updating" >&2
        api_request PATCH "$URL/$ID" "$BODY" | jq
        ;;
      *)
        echo "k0r.sh: GET $URL/$ID returned HTTP $HTTP_CODE" >&2
        cat "$GET_RESP" >&2
        exit 1
        ;;
    esac
    rm -f "$GET_RESP"
    trap - EXIT
    ;;
  delete)
    if [ "${3:-}" = "--file" ]; then
      FILE="${4:?usage: $0 delete <resource-path> --file <body.json|body.yaml>}"
      ID=$(id_from_file "$FILE")
    else
      ID="${3:?usage: $0 delete <resource-path> <id> | $0 delete <resource-path> --file <body.json|body.yaml>}"
    fi
    curl -s -X DELETE -H "Authorization: Bearer $TOKEN" -o /dev/null -w '%{http_code}\n' \
      "$URL/$ID"
    ;;
  *)
    usage
    ;;
esac
