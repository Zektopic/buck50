#!/usr/bin/env bash
#
# Start a single-use GitHub Actions runner for this repository, in Docker,
# so the "Firmware release" workflow (.github/workflows/release.yml) can
# build and publish firmware on this machine.
#
#   tools/selfhosted-runner.sh
#
# Then push a release tag (git tag v0.9.3 && git push origin v0.9.3) or use
# "Run workflow" in the Actions tab. Either order works: a queued job waits
# for a runner. The runner is registered to this repository only, runs one
# job (--ephemeral), and is removed from GitHub after that job or when this
# script is interrupted (CTRL-C).
#
# Needs: docker, and gh logged in with admin access to the repository
# (to get a runner registration token).
#
# SECURITY: the job builds with this machine's Docker daemon (the runner
# gets /var/run/docker.sock), which amounts to root access to this machine.
# Only run this when you mean to run a workflow from this repository.
# The release workflow runs only for tag pushes and manual runs, which
# need write access; GitHub-hosted runners handle pull requests.
#
# Environment overrides:
#   REPO          owner/name           (default: this checkout's GitHub repo)
#   LABELS        extra runner labels  (default: manupa-hnx,buck50)
#   RUNNER_IMAGE  runner image         (default: pinned official image)
#   CPUS          CPU limit for the runner container (default: 2). Docker
#                 builds run in the Docker daemon and aren't limited by it;
#                 the firmware build and tests use about one core.

set -euo pipefail

REPO=${REPO:-$(gh repo view --json nameWithOwner --jq .nameWithOwner)}
LABELS=${LABELS:-manupa-hnx,buck50}
RUNNER_IMAGE=${RUNNER_IMAGE:-ghcr.io/actions/actions-runner:2.338.2}
CPUS=${CPUS:-2}
NAME=${NAME:-buck50-$(hostname -s)-$(date +%Y%m%d-%H%M%S)}
SOCK=/var/run/docker.sock

remove_runner() {
    local id
    id=$(gh api "repos/$REPO/actions/runners" --paginate \
             --jq ".runners[] | select(.name == \"$NAME\") | .id" \
             2>/dev/null || true)
    if [ -n "$id" ]; then
        gh api -X DELETE "repos/$REPO/actions/runners/$id" >/dev/null \
            && echo "Removed runner $NAME from $REPO"
    fi
}

cleanup() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    remove_runner
}
trap cleanup EXIT

RUNNER_TOKEN=$(gh api -X POST "repos/$REPO/actions/runners/registration-token" \
                   --jq .token)
export RUNNER_TOKEN

echo "Starting single-use runner $NAME for $REPO (labels: self-hosted,$LABELS)"
echo "It runs one job, then exits. CTRL-C to stop it without running a job."

docker run --rm --name "$NAME" --cpus "$CPUS" \
    -v "$SOCK:$SOCK" --group-add "$(stat -c %g "$SOCK")" \
    -e RUNNER_TOKEN -e REPO="$REPO" -e NAME="$NAME" -e LABELS="$LABELS" \
    "$RUNNER_IMAGE" \
    bash -c './config.sh --unattended --ephemeral --disableupdate --replace \
                         --url "https://github.com/$REPO" \
                         --token "$RUNNER_TOKEN" \
                         --name "$NAME" --labels "$LABELS" --work _work \
              && exec ./run.sh'
