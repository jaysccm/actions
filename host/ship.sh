#!/usr/bin/env bash
# Build-host side of a release: ship.sh <project> <commit> <image> <target-host>
# Streams an already built and tested image to the production host and checks
# that it arrived byte-identical (same image ID). It never builds on the target.
# The target is reached with the dedicated, restricted key $SHIP_KEY (default
# ~/.ssh/id_ed25519_ship) whose authorized_keys entry on the target allows only
# the ship-receive.sh forced command.
set -euo pipefail

project=${1:?usage: ship.sh <project> <commit> <image> <target-host>}
commit=${2:?}
image=${3:?}
target=${4:?}
key=${SHIP_KEY:-$HOME/.ssh/id_ed25519_ship}

local_id=$(docker image inspect --format '{{.Id}}' "$image")
echo "Shipping $image ($local_id) for $project@$commit to $target"

docker save "$image" | gzip -1 |
  ssh -i "$key" -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes \
    "ship@$target" "$project" "$commit" "$image" "$local_id"
