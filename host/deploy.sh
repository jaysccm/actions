#!/usr/bin/env bash
# Deploy one project on this host: deploy.sh <project> <commit>
# Called by the GitHub runner after CI passes; safe to run by hand.
set -uo pipefail

project=${1:?usage: deploy.sh <project> <commit>}
ref=${2:?usage: deploy.sh <project> <commit>}
dir="$HOME/services/$project"

# Throwaway project for rehearsing release.yml: no checkout, no service.
if [ "$project" = dryrun ]; then
  [ -n "${DEPLOY_IMAGE:-}" ] && docker image inspect "$DEPLOY_IMAGE" >/dev/null 2>&1 ||
    { echo "dryrun: shipped image ${DEPLOY_IMAGE:-<unset>} not on this host" >&2; exit 4; }
  echo "dryrun: $(hostname) has $DEPLOY_IMAGE ($(docker image inspect --format '{{.Id}}' "$DEPLOY_IMAGE")); no build ran here"
  docker rmi "$DEPLOY_IMAGE" >/dev/null
  exit 0
fi

[ -d "$dir/.git" ] || { echo "No checkout at $dir" >&2; exit 2; }
cd "$dir" || exit 2

# One deploy at a time on this host.
exec 9>"$HOME/services/.deploy.lock"
flock -n 9 || { echo "Another deploy is running" >&2; exit 3; }

# Release mode (release.yml): the tested image was built on the build host and
# loaded here; production must not build. Recipes use $DEPLOY_IMAGE instead of a
# local build; a recipe that cannot do this must refuse, not rebuild.
release_mode=0
[ -n "${DEPLOY_IMAGE:-}" ] && release_mode=1
docker image inspect "${DEPLOY_IMAGE:-none}" >/dev/null 2>&1 || [ "$release_mode" = 0 ] || {
  echo "Image $DEPLOY_IMAGE not found on this host" >&2; exit 4; }

case "$project" in
  sportsbook)
    # Everything is Docker Compose (JAY-117): the project's script stops the collector and
    # the scheduled jobs, fast-forwards, builds (or loads the prebuilt image), migrates,
    # recreates the API and brings back what was running, rolling back itself on failure.
    # No systemd and no sudo: the only access is Docker. Needs scripts/deploy.py, so a
    # host still on the systemd setup must be moved first (docs/guides/deployment.md).
    rc=0
    # In release mode the image was built and tested on the build host and is
    # already loaded here; the updater verifies its commit and never builds.
    if [ "$release_mode" = 1 ]; then
      python3 scripts/deploy.py update --ref "$ref" --wait --image "$DEPLOY_IMAGE" || rc=1
    else
      python3 scripts/deploy.py update --ref "$ref" --wait || rc=1
    fi

    python3 scripts/deploy.py status ||
      echo "::warning::deployment status reported warnings (see log above)"
    exit "$rc"
    ;;
  telemetry)
    # Config-only stack: the project's updater moves the checkout to the commit, recreates the stack, waits for
    # Grafana to be healthy and rolls back itself on failure. A stack that was stopped on purpose stays stopped.
    rc=0
    ./update.sh "$ref" || rc=1

    ./status.sh || echo "::warning::telemetry stack is not healthy (see log above)"
    exit "$rc"
    ;;
  *)
    echo "No deploy recipe for $project" >&2
    exit 2
    ;;
esac
