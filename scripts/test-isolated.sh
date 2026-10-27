#!/bin/sh
# Run only in an unprivileged private PID namespace, including on a Docker-backed Mac.
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
stamp=$(date -u +%Y%m%d%H%M%S)-$$
image="aos-ci:check-$stamp"
created=0
cleanup() {
    if [ "$created" = 1 ]; then docker image rm "$image" >/dev/null 2>&1 || true; fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
if docker image inspect "$image" >/dev/null 2>&1; then
    echo 'Refusing an existing check image tag' >&2
    exit 1
fi
docker build --file "$root/.github/ci.Dockerfile" --tag "$image" "$root"
created=1
# Read-only probe, no tests or process signals in this namespace.
host_ns=$(docker run --rm --pid=host --entrypoint=readlink "$image" /proc/self/ns/pid)
case "$host_ns" in pid:\[*\]) ;; *) echo 'Cannot identify Docker host PID namespace' >&2; exit 1;; esac
if [ "$#" = 0 ]; then set -- -q; fi
docker run --rm --init --user 1000:1000 --cpus 2 --memory 2g --pids-limit 256 \
    --env "AOS_TEST_HOST_PID_NS=$host_ns" --entrypoint sh "$image" -eu -c '
    current_ns=$(readlink /proc/self/ns/pid)
    test "$(id -u)" = 1000
    test "$current_ns" != "$AOS_TEST_HOST_PID_NS"
    printf "Test isolation: uid=%s host=%s container=%s\n" "$(id -u)" "$AOS_TEST_HOST_PID_NS" "$current_ns"
    exec python -m pytest "$@"
' isolated-pytest "$@"
