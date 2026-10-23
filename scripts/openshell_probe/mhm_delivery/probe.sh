#!/bin/sh
# Host side: probe.sh <image> '<shell snippet run inside the container>'
# Creates a container with no network, copies this directory to /probe, runs the snippet, copies /tmp/cases back to
# $OUT (default /tmp/mhm-cases/<timestamp>) and removes the container. The snippet runs the case runners, e.g.
#   SEED='[{"from":"operator","body":"SEEDED-AT-START"}]' sh /probe/run-omp.sh c1
image=${1:?usage: probe.sh <image> '<snippet>'}; shift
here=$(cd "$(dirname "$0")" && pwd)
out=${OUT:-/tmp/mhm-cases/$(date +%Y%m%d-%H%M%S)}
cid=$(docker create --network none --entrypoint sh "$image" -c "$*") || exit 1
agent_tmp=$(mktemp -d) || { docker rm "$cid" >/dev/null; exit 1; }
trap 'rm -rf "$agent_tmp"' 0
cp "$here/../../../docker/harness/aos_agent.py" "$agent_tmp/aos-agent" && chmod 755 "$agent_tmp/aos-agent" || { docker rm "$cid" >/dev/null; exit 1; }
docker cp "$here/." "$cid:/probe" >/dev/null || { docker rm "$cid" >/dev/null; exit 1; }
docker cp "$agent_tmp/aos-agent" "$cid:/probe/aos-agent" >/dev/null || { docker rm "$cid" >/dev/null; exit 1; }
rm -rf "$agent_tmp"
trap - 0
docker start -a "$cid"
rc=$?
mkdir -p "$out" && docker cp "$cid:/tmp/cases/." "$out/" >/dev/null 2>&1
docker rm "$cid" >/dev/null
echo "cases copied to $out"
exit $rc
