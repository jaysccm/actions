#!/usr/bin/env bash
# Production-host side of ship.sh, installed on the target and bound to the ship
# key with: command="/home/ship/ship-receive.sh",restrict ssh-ed25519 ...
# Arguments arrive in $SSH_ORIGINAL_COMMAND: <project> <commit> <image> <image-id>.
# Loads the image and fails unless the loaded image ID equals the build host's.
# It cannot run anything else and gives no deployment authority.
set -euo pipefail

read -r project commit image want_id extra <<<"${SSH_ORIGINAL_COMMAND:-}"
[ -z "${extra:-}" ] && [ -n "${want_id:-}" ] || { echo "usage: <project> <commit> <image> <image-id>" >&2; exit 2; }
[[ $project =~ ^[a-z0-9][a-z0-9_-]*$ ]] || { echo "bad project" >&2; exit 2; }
[[ $commit =~ ^[0-9a-f]{7,40}$ ]] || { echo "bad commit" >&2; exit 2; }
[[ $image == "$project:$commit"* ]] || { echo "image must be $project:<commit>" >&2; exit 2; }
[[ $want_id =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "bad image id" >&2; exit 2; }

gunzip | docker load >/dev/null
got_id=$(docker image inspect --format '{{.Id}}' "$image")
[ "$got_id" = "$want_id" ] || { echo "image id mismatch: $got_id != $want_id" >&2; exit 1; }
echo "loaded $image ($got_id)"
