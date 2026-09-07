#!/usr/bin/env bash
# Download chart_version.txt + image_tag.txt from the latest successful
# workflow run on TARGET_BRANCH (same pattern as k0rdent/core-services
# e2e-periodic-ufo). Default branch is main.
set -euo pipefail

: "${TARGET_REPO:?}"
: "${WORKFLOW_ID:?}"
: "${GH_TOKEN:?}"

target_branch="${TARGET_BRANCH:-main}"
max_attempts="${MAX_ATTEMPTS:-10}"
outdir="${OUT_DIR:-.}"
mkdir -p "$outdir"

for attempt in $(seq 1 "$max_attempts"); do
  echo "Attempt $attempt/$max_attempts for $TARGET_REPO ($WORKFLOW_ID) branch=$target_branch"
  rm -vf "$outdir/chart_version.txt" "$outdir/image_tag.txt"

  RUN_ID=$(gh api \
    "repos/$TARGET_REPO/actions/workflows/$WORKFLOW_ID/runs?branch=${target_branch}&status=success" \
    --jq '.workflow_runs[0].id') || RUN_ID=""

  if [ -n "$RUN_ID" ] && [ "$RUN_ID" != "null" ] \
    && gh run download "$RUN_ID" -R "$TARGET_REPO" -n build-chart-version -D "$outdir" \
    && gh run download "$RUN_ID" -R "$TARGET_REPO" -n build-image-tag -D "$outdir"; then
    chart_version="$(tr -d '[:space:]' <"$outdir/chart_version.txt")"
    image_version="$(tr -d '[:space:]' <"$outdir/image_tag.txt")"
    echo "chart_version=${chart_version}"
    echo "image_version=${image_version}"
    if [ -n "${GITHUB_OUTPUT:-}" ]; then
      {
        echo "chart_version=${chart_version}"
        echo "image_version=${image_version}"
      } >>"$GITHUB_OUTPUT"
    fi
    exit 0
  fi

  if [ "$attempt" -lt "$max_attempts" ]; then
    echo "Failed, retrying in 30s..."
    sleep 30
  fi
done

echo "All attempts failed for $TARGET_REPO branch=$target_branch"
exit 1
