#!/usr/bin/env bash
# Deploy one project on this host: deploy.sh <project> <commit>
# Called by the GitHub runner after CI passes; safe to run by hand.
set -uo pipefail

project=${1:?usage: deploy.sh <project> <commit>}
ref=${2:?usage: deploy.sh <project> <commit>}
dir="$HOME/services/$project"

[ -d "$dir/.git" ] || { echo "No checkout at $dir" >&2; exit 2; }
cd "$dir" || exit 2

# One deploy at a time on this host.
exec 9>"$HOME/services/.deploy.lock"
flock -n 9 || { echo "Another deploy is running" >&2; exit 3; }

case "$project" in
  sportsbook)
    # The project's updater pauses jobs, fast-forwards, rebuilds, migrates,
    # recreates the API and waits for it to be healthy; on failure it restores
    # the previous commit and image itself. It always leaves the collector
    # stopped, so put the collector back if it was running (Jay, 2026-10-03:
    # deploys may restart the collector).
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
