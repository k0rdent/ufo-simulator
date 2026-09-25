#!/usr/bin/env bash
# Download chart_version.txt + image_tag.txt from the workflow run that built
# the head (latest merged) commit of TARGET_BRANCH (same pattern as
# k0rdent/core-services e2e-periodic-ufo). Default branch is main.
# Waits while that run is queued or in progress; fails if it did not succeed.
set -euo pipefail

: "${TARGET_REPO:?}"
: "${WORKFLOW_ID:?}"
: "${GH_TOKEN:?}"

target_branch="${TARGET_BRANCH:-main}"
max_attempts="${MAX_ATTEMPTS:-30}"
max_pages="${MAX_PAGES:-5}"
outdir="${OUT_DIR:-.}"
mkdir -p "$outdir"

# Prints the id of the successful push run for the branch head commit.
# Returns 1 when it should be retried (not started yet, still running, API
# error) and 2 when the run finished without success.
# The runs API's branch/status/head_sha filters are served from a search index
# that can lag, so list unfiltered runs (newest first) and match them here.
head_run_id() {
  local head_sha page run id status conclusion
  head_sha=$(gh api "repos/$TARGET_REPO/commits/$target_branch" --jq '.sha') || return 1
  for page in $(seq 1 "$max_pages"); do
    run=$(gh api "repos/$TARGET_REPO/actions/workflows/$WORKFLOW_ID/runs?per_page=100&page=${page}" \
      | jq -r --arg branch "$target_branch" --arg repo "$TARGET_REPO" --arg sha "$head_sha" '
          [.workflow_runs[]
            | select(.head_sha == $sha
                and .head_branch == $branch
                and .event == "push"
                and .head_repository.full_name == $repo)]
          | max_by(.created_at)
          | if . then "\(.id) \(.status) \(.conclusion) #\(.run_number) \(.html_url)" else empty end') || return 1
    if [ -n "$run" ]; then
      read -r id status conclusion _ <<<"$run"
      echo "Head ${head_sha:0:7} of $target_branch: run $run" >&2
      if [ "$status" != "completed" ]; then
        echo "Run $id is $status, waiting" >&2
        return 1
      fi
      if [ "$conclusion" != "success" ]; then
        echo "Run $id for head ${head_sha:0:7} of $target_branch finished with $conclusion" >&2
        return 2
      fi
      echo "$id"
      return 0
    fi
  done
  echo "No push run for head ${head_sha:0:7} of $target_branch yet" >&2
  return 1
}

for attempt in $(seq 1 "$max_attempts"); do
  echo "Attempt $attempt/$max_attempts for $TARGET_REPO ($WORKFLOW_ID) branch=$target_branch"
  rm -vf "$outdir/chart_version.txt" "$outdir/image_tag.txt"

  rc=0
  RUN_ID=$(head_run_id) || rc=$?
  if [ "$rc" -eq 2 ]; then
    echo "Build of the latest $target_branch commit failed for $TARGET_REPO"
    exit 1
  fi

  if [ "$rc" -eq 0 ] \
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
    echo "Not ready, retrying in 30s..."
    sleep 30
  fi
done

echo "All attempts failed for $TARGET_REPO branch=$target_branch"
exit 1
