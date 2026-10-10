#!/usr/bin/env bash
# Run a CI step inside a throwaway container on the runner's Docker daemon.
#
# The organisation's self-hosted runners are themselves containers that drive
# the host's Docker daemon. GitHub's `container:` jobs therefore fail (the
# runner's /__e externals are not on the host, so node cannot start in the job
# container), and bind-mounting the workspace does not work either. Instead the
# checkout is copied into the container, as the org's other repos do.
#
# usage: in-container.sh IMAGE WORKDIR SCRIPT [extra `docker create` args...]
#   WORKDIR is relative to the repository root; SCRIPT runs under `sh -ec`.
set -euo pipefail

image=$1 workdir=$2 script=$3
shift 3

name="ci-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-0}-${GITHUB_JOB:-job}-$$"
cleanup() { docker rm -f "$name" >/dev/null 2>&1 || true; }
trap cleanup EXIT

docker create --name "$name" -w "/workspace/$workdir" -e HOME=/tmp "$@" \
  "$image" sh -ec "$script" >/dev/null
docker cp "$PWD/." "$name:/workspace"
docker start -a "$name"
