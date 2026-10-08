# shellcheck shell=sh
# Shared helpers for the standard scripts. Sourced, never run. Plain POSIX sh.
#
# Exit codes (the contract, same for every script):
#   0  success
#   1  a step failed (a hook exited non-zero, a build, migration or health check failed)
#   2  usage error or failed preflight; nothing was changed
#   3  another run holds the lock
#   4  status only: the project is running but unhealthy
#
# Environment handed to every ext hook:
#   PROJECT       project name (default: basename of the repo root)
#   INSTANCE      optional instance name for multi-instance projects (empty if none)
#   REF           git ref being deployed (update/deploy), else empty
#   PREVIOUS_REF  commit the checkout was on before this run (update/deploy), else empty
#   DEPLOY_IMAGE  prebuilt image to run instead of building (update/deploy), else empty
#   ENV_FILE      path to the gitignored env file (default: <root>/.env)
#   STRTA_STEP    the standard script that is running the hook
#   STRTA_AS      optional command prefix (e.g. 'sudo -u owner -H') for writing to the checkout; set in script/ext/env
#   STRTA_WAIT    seconds the verify hook may wait for health (update only; default 180)

STRTA_EXIT_FAIL=1
STRTA_EXIT_USAGE=2
STRTA_EXIT_LOCKED=3
STRTA_EXIT_UNHEALTHY=4

STRTA_ROOT=$(cd "$(dirname "$0")/.." && pwd)
PROJECT=${PROJECT:-$(basename "$STRTA_ROOT")}
INSTANCE=${INSTANCE:-}
ENV_FILE=${ENV_FILE:-$STRTA_ROOT/.env}
REF=${REF:-}
PREVIOUS_REF=${PREVIOUS_REF:-}
DEPLOY_IMAGE=${DEPLOY_IMAGE:-}
STRTA_AS=${STRTA_AS:-}

# Optional project settings. script/ext/env is sourced (not run as a hook) and may set PROJECT, INSTANCE, ENV_FILE
# and STRTA_AS, e.g. STRTA_AS='sudo -u pearl -H' when the checkout belongs to another user.
# shellcheck disable=SC1091
[ -f "$STRTA_ROOT/script/ext/env" ] && . "$STRTA_ROOT/script/ext/env"
export PROJECT INSTANCE ENV_FILE REF PREVIOUS_REF DEPLOY_IMAGE

# as_owner <command...>: run a command that reads or writes the checkout as its owner (see STRTA_AS).
# The standard scripts use it for every git call and for creating the env file; hooks run as the invoking user.
as_owner() { if [ -n "$STRTA_AS" ]; then $STRTA_AS "$@"; else "$@"; fi; }

log() { printf '%s: %s\n' "$(basename "$0")" "$*"; }
warn() { printf '%s: warning: %s\n' "$(basename "$0")" "$*" >&2; }
die() { code=$1; shift; printf '%s: error: %s\n' "$(basename "$0")" "$*" >&2; exit "$code"; }
usage_error() { printf '%s: %s\n(see --help)\n' "$(basename "$0")" "$*" >&2; exit "$STRTA_EXIT_USAGE"; }

# run_hook <name>: run script/ext/<name> if present and executable; skip silently otherwise.
# A non-zero exit fails the step. Returns the hook's status; callers decide how to unwind.
run_hook() {
  hook="$STRTA_ROOT/script/ext/$1"
  [ -f "$hook" ] && [ -x "$hook" ] || return 0
  log "hook $1"
  STRTA_STEP=$(basename "$0") "$hook"
}

has_hook() { [ -f "$STRTA_ROOT/script/ext/$1" ] && [ -x "$STRTA_ROOT/script/ext/$1" ]; }

# lock <name>: take an exclusive non-blocking lock shared by every run of this project on the host.
# Held until the process exits. Uses flock when available, else an atomic mkdir lock.
lock() {
  lockbase=${STRTA_LOCK_DIR:-${TMPDIR:-/tmp}}/strta-$PROJECT${INSTANCE:+-$INSTANCE}
  if command -v flock >/dev/null 2>&1; then
    exec 9>"$lockbase.lock"
    flock -n 9 || die "$STRTA_EXIT_LOCKED" "another run holds $lockbase.lock"
  else
    mkdir "$lockbase.lockdir" 2>/dev/null || die "$STRTA_EXIT_LOCKED" "another run holds $lockbase.lockdir"
    # shellcheck disable=SC2064
    trap "rmdir '$lockbase.lockdir'" EXIT
  fi
}

# wait_until <seconds> <command...>: poll every second until the command succeeds.
wait_until() {
  limit=$1; shift
  waited=0
  while ! "$@" >/dev/null 2>&1; do
    [ "$waited" -lt "$limit" ] || return 1
    sleep 1
    waited=$((waited + 1))
  done
}
