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
# local build. A recipe that cannot do this yet must refuse, not rebuild.
release_mode=0
[ -n "${DEPLOY_IMAGE:-}" ] && release_mode=1
docker image inspect "${DEPLOY_IMAGE:-none}" >/dev/null 2>&1 || [ "$release_mode" = 0 ] || {
  echo "Image $DEPLOY_IMAGE not found on this host" >&2; exit 4; }

case "$project" in
  sportsbook)
    # The project's updater pauses jobs, fast-forwards, rebuilds, migrates,
    # recreates the API and waits for it to be healthy; on failure it restores
    # the previous commit and image itself. It always leaves the collector
    # stopped, so put the collector back if it was running (Jay, 2026-10-03:
    # deploys may restart the collector).
    if [ "$release_mode" = 1 ]; then
      echo "sportsbook: its updater has no prebuilt-image option yet (JAY-110 4.2); refusing to rebuild on production" >&2
      exit 5
    fi
    collector_was_active=0
    systemctl is-active --quiet gamblor-collector.service && collector_was_active=1

    rc=0
    python3 scripts/setup_wsl.py update --ref "$ref" --wait || rc=1

    if [ "$collector_was_active" = 1 ]; then
      python3 scripts/setup_wsl.py activate-collector || {
        echo "::error::collector was running before the deploy and could not be restarted" >&2
        rc=1
      }
    fi

    python3 scripts/setup_wsl.py status ||
      echo "::warning::deployment status reported warnings (see log above)"
    exit "$rc"
    ;;
  *)
    echo "No deploy recipe for $project" >&2
    exit 2
    ;;
esac
