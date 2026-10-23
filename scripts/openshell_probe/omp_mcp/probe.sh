#!/bin/sh
# Host side: probe.sh <image>. A container with no network (the fake model is on its loopback); the kit is copied in
# (Colima does not mount every host path). About 6 minutes, most of it the two 130 s calls.
image=${1:?usage: probe.sh <image with omp and aos-agent>}
here=$(cd "$(dirname "$0")" && pwd)
cid=$(docker create --network none --entrypoint sh "$image" /probe/cases.sh) || exit 1
docker cp "$here/." "$cid:/probe" >/dev/null && docker start -a "$cid"
docker rm "$cid" >/dev/null
