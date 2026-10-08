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

# Pipeline telemetry (JAY-122): optional and non-blocking. A deploy only counts as successful when the
# updater exited 0, a running container of this project is the tested image, and none is unhealthy;
# the job exit status alone is not enough. It is recorded, and fails the job only when
# DEPLOY_VERIFY_ENFORCE=1. Needs OTEL_EXPORTER_OTLP_ENDPOINT and pipeline_metrics.py beside this script.
pm_script="$(dirname "$0")/pipeline_metrics.py"
pm_emit() {
  [ -f "$pm_script" ] && [ -n "${OTEL_EXPORTER_OTLP_ENDPOINT:-}" ] || return 0
  timeout 20 python3 "$pm_script" record --project "$project" --env "${PIPELINE_ENV:-production}" "$@" || true
}
pm_images() {
  { docker ps -q --filter "label=com.docker.compose.project=$project" 2>/dev/null |
    xargs -r docker inspect --format '{{.Image}}' 2>/dev/null; } | sort -u
}
pm_healthy() {
  local ids states
  ids=$(docker ps -q --filter "label=com.docker.compose.project=$project" 2>/dev/null) && [ -n "$ids" ] || return 1
  # shellcheck disable=SC2086
  states=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' $ids 2>/dev/null) || return 1
  ! grep -qE '^(unhealthy|starting)$' <<<"$states"
}
pm_started=$(date +%s)
pm_before=$(pm_images 2>/dev/null || true)
pm_finish() {
  local rc=$? elapsed verify=skipped health=failure good=0 images
  trap - EXIT
  elapsed=$(( $(date +%s) - pm_started ))
  if [ "$rc" = 130 ] || [ "$rc" = 143 ]; then
    pm_emit job stage=deploy outcome=canceled
  elif [ "$rc" = 0 ]; then
    if [ "$release_mode" = 1 ]; then
      images=$(pm_images)
      if [ -z "$images" ]; then verify=error
      elif grep -Fxq "${EXPECTED_IMAGE_ID:-none}" <<<"$images"; then verify=ok
      else verify=mismatch; fi
    fi
    pm_healthy && health=success
    if [ "$health" = success ] && { [ "$release_mode" = 0 ] || [ "$verify" = ok ]; }; then good=1; fi
    pm_emit job stage=health outcome=$health
    [ "$release_mode" = 1 ] && pm_emit verify point=target outcome=$verify
    if [ "$good" = 1 ]; then
      pm_emit job stage=deploy outcome=success duration=$elapsed
    else
      pm_emit job stage=deploy outcome=failure duration=$elapsed
      echo "::warning::deploy exited 0 but is not verified (artifact check: $verify, health: $health)" >&2
      [ "${DEPLOY_VERIFY_ENFORCE:-0}" = 1 ] && rc=1
    fi
  else
    pm_emit job stage=deploy outcome=failure duration=$elapsed
    # The updater must put the previous version back; record whether that is what is running and healthy.
    if [ -n "$pm_before" ]; then
      if [ "$(pm_images)" = "$pm_before" ] && pm_healthy; then pm_emit rollback outcome=success
      else pm_emit rollback outcome=failure; fi
    fi
  fi
  exit "$rc"
}
trap pm_finish EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

# Projects that ship the standard scripts (JAY-147) deploy through their own script/deploy: update, health wait,
# rollback and status in one contract. Not exec'd, so the metrics trap above still runs. DEPLOY_IMAGE and
# EXPECTED_IMAGE_ID stay in the environment for the project's hooks, which must enforce any release-mode rule the
# case branches below enforce today (algo: a verified image and its expected ID). Unlike the legacy recipes, an
# unhealthy final status fails the deploy.
if [ -x script/deploy ]; then
  script/deploy "$ref" ${DEPLOY_IMAGE:+--image "$DEPLOY_IMAGE"}
  exit $?
fi

# Legacy recipes for projects without script/deploy; remove each as its project adopts the scripts.
case "$project" in
  algo)
    # Pinchy is intentionally deployed on bee. Its updater requires the exact
    # image ID from release.yml and preserves HALT and timer state itself.
    if [ "$release_mode" != 1 ] || [ "$DEPLOY_IMAGE" != "algo:$ref" ] || [ -z "${EXPECTED_IMAGE_ID:-}" ]; then
      echo "algo requires a verified release image and its expected image ID" >&2
      exit 4
    fi
    rc=0
    python3 scripts/update.py "$ref" --expected-image-id "$EXPECTED_IMAGE_ID" || rc=1
    python3 scripts/status.py ||
      echo "::warning::Pinchy status could not be reported (see log above)"
    exit "$rc"
    ;;
  sportsbook)
    if [ -f scripts/deploy.py ]; then
      # Compose-native updater (JAY-117). In release mode it uses the tested
      # image already loaded on this host and does not rebuild.
      rc=0
      if [ "$release_mode" = 1 ]; then
        python3 scripts/deploy.py update --ref "$ref" --wait --image "$DEPLOY_IMAGE" || rc=1
      else
        python3 scripts/deploy.py update --ref "$ref" --wait || rc=1
      fi
      python3 scripts/deploy.py status ||
        echo "::warning::deployment status reported warnings (see log above)"
      exit "$rc"
    fi
    # Bee is still on the older systemd-backed Sportsbook checkout. Preserve
    # its installed deploy path until that project is migrated to JAY-117.
    if [ -f scripts/setup_wsl.py ] && [ "$release_mode" = 0 ]; then
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
    fi
    echo "Sportsbook checkout lacks a compatible updater for this deploy mode" >&2
    exit 4
    ;;
  telemetry)
    # TRANSITION ONLY: the checkout on the host is still the commit before script/ existed, so the first deploy of the
    # commit that adds script/ (telemetry PR #13) needs this recipe; it removes update.sh/status.sh, so the status call
    # below warns once. Delete this branch after that deploy. Config-only stack: the project's updater moves the checkout to the commit, recreates the stack, waits for
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
