#!/usr/bin/env bash
# Force-clear every namespaced resource inside a namespace (namespace is kept).
#
# 1. Deletes all deletable namespaced resources with --wait=false.
# 2. Clears finalizers on any objects that remain (so they can finish terminating).
#
# Usage:
#   force-wipe-ns.sh <namespace>
#
# Requires: kubectl, jq

set -euo pipefail

usage() {
  echo "usage: $0 <namespace>" >&2
  exit 1
}

NS="${1:-}"
[ -n "$NS" ] || usage

if ! command -v kubectl >/dev/null 2>&1; then
  echo "$0: kubectl not found on PATH" >&2
  exit 1
fi
if ! command -v jq >/dev/null 2>&1; then
  echo "$0: jq not found on PATH" >&2
  exit 1
fi

if ! kubectl get namespace "$NS" >/dev/null 2>&1; then
  echo "$0: namespace $NS not found" >&2
  exit 1
fi

# All namespaced resource types that support delete (built-ins + CRDs).
namespaced_resources() {
  kubectl api-resources --namespaced=true --verbs=delete -o name 2>/dev/null | sort -u
}

echo "Deleting all resources in namespace $NS (not waiting for termination)..."
deleted_types=0
while IFS= read -r resource; do
  [ -n "$resource" ] || continue
  if kubectl delete "$resource" --all -n "$NS" --wait=false --ignore-not-found >/dev/null 2>&1; then
    deleted_types=$((deleted_types + 1))
  fi
done <<EOF
$(namespaced_resources)
EOF
echo "  issued delete --wait=false for $deleted_types resource type(s)"

echo "Clearing finalizers on remaining objects in $NS..."
cleared=0
while IFS= read -r resource; do
  [ -n "$resource" ] || continue

  names="$(kubectl get "$resource" -n "$NS" -o json 2>/dev/null |
    jq -r '.items[]? | select((.metadata.finalizers // []) | length > 0) | .metadata.name' || true)"
  [ -n "$names" ] || continue

  while IFS= read -r name; do
    [ -n "$name" ] || continue
    echo "  clearing finalizers: $resource/$name"
    if kubectl patch "$resource" "$name" -n "$NS" --type=json \
      -p='[{"op":"remove","path":"/metadata/finalizers"}]' >/dev/null 2>&1; then
      :
    elif kubectl patch "$resource" "$name" -n "$NS" \
      --type=merge -p '{"metadata":{"finalizers":null}}' >/dev/null 2>&1; then
      :
    else
      echo "  warning: failed to clear finalizers on $resource/$name" >&2
      continue
    fi
    cleared=$((cleared + 1))
  done <<EOF
$names
EOF
done <<EOF
$(namespaced_resources)
EOF

echo "Done. Cleared finalizers on $cleared object(s). Namespace $NS was not deleted."
echo "Check remaining objects with: kubectl api-resources --namespaced -o name | xargs -n1 -I{} kubectl get {} -n $NS --ignore-not-found 2>/dev/null"
