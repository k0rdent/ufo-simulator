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
max_pages="${MAX_PAGES:-5}"
outdir="${OUT_DIR:-.}"
mkdir -p "$outdir"

# The runs API's branch/status filters are served from a search index that can
# lag and return an older run first, so list unfiltered runs (newest first) and
# pick the newest successful non-PR run of this repo's branch ourselves.
latest_run_id() {
  local page id
  for page in $(seq 1 "$max_pages"); do
    id=$(gh api "repos/$TARGET_REPO/actions/workflows/$WORKFLOW_ID/runs?per_page=100&page=${page}" \
      | jq -r --arg branch "$target_branch" --arg repo "$TARGET_REPO" '
          [.workflow_runs[]
            | select(.head_branch == $branch
                and .conclusion == "success"
                and .event != "pull_request"
                and .head_repository.full_name == $repo)]
          | max_by(.created_at)
          | if . then "\(.id) #\(.run_number) \(.head_sha[0:7]) \(.created_at)" else empty end') || return 1
    if [ -n "$id" ]; then
      echo "Selected run: $id" >&2
      echo "${id%% *}"
      return 0
    fi
  done
  echo "No successful run on $target_branch in the last $max_pages pages" >&2
  return 1
}

for attempt in $(seq 1 "$max_attempts"); do
  echo "Attempt $attempt/$max_attempts for $TARGET_REPO ($WORKFLOW_ID) branch=$target_branch"
  rm -vf "$outdir/chart_version.txt" "$outdir/image_tag.txt"

  RUN_ID=$(latest_run_id) || RUN_ID=""

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
