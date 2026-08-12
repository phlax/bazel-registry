#!/usr/bin/env bash
# Local/CI runner for BCR-style module verification (CI tier 2).
#
# Builds the verification container and runs a single module version
# against it. The registry is bind-mounted read-only, the scratch
# consumer workspace stays container-internal, and artifacts
# (build logs + build-event JSON) land in a narrow writable mount.
#
# The disk/repository cache lives on a named volume for tolerable local
# iteration; --cold forces the cold path (which is what CI tests).
#
# Usage:
#   verify/verify.sh [options] <module> <version>
#
# Options:
#   --bazel <version>     Bazel version(s), comma-separated (default: 8.x)
#   --cold                No cache volume - cold build, as CI runs it
#   --artifacts <dir>     Artifacts output dir (default: ./verify-artifacts)
#   --image <tag>         Image tag (default: envoy-bazel-registry-verify)
#   --no-build            Skip docker build (use an existing image)
#
# RBE is opt-in: set RBE=1 and RBE_CREDS=<dir> (bind-mounted read-only at
# /rbe, expected to contain a `bazelrc` snippet). Nothing is baked into
# the image and the default (off) works from a fresh clone with zero
# setup.

set -e -o pipefail

REGISTRY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE=envoy-bazel-registry-verify
CACHE_VOLUME=envoy-bazel-registry-verify-cache
ARTIFACTS="${PWD}/verify-artifacts"
BAZEL_VERSIONS="${BAZEL_VERSIONS:-8.x}"
COLD=
BUILD_IMAGE=1
ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --bazel) BAZEL_VERSIONS="$2"; shift 2 ;;
        --cold) COLD=1; shift ;;
        --artifacts) ARTIFACTS="$2"; shift 2 ;;
        --image) IMAGE="$2"; shift 2 ;;
        --no-build) BUILD_IMAGE=; shift ;;
        -h|--help) grep '^#' "$0" | cut -c3-; exit 0 ;;
        *) ARGS+=("$1"); shift ;;
    esac
done

if [[ ${#ARGS[@]} -ne 2 ]]; then
    echo "Usage: $0 [options] <module> <version>" >&2
    exit 1
fi
MODULE="${ARGS[0]}"
VERSION="${ARGS[1]}"

if [[ ! -d "${REGISTRY_ROOT}/modules/${MODULE}/${VERSION}" ]]; then
    echo "No such module version: modules/${MODULE}/${VERSION}" >&2
    exit 1
fi

if [[ -n "$BUILD_IMAGE" ]]; then
    docker build -t "$IMAGE" "${REGISTRY_ROOT}/verify"
fi

mkdir -p "$ARTIFACTS"
ARTIFACTS="$(cd "$ARTIFACTS" && pwd)"

# uid/gid is a runtime concern (not baked into the image) so images are
# potentially reusable - run as the invoking user with a writable HOME.
RUN_USER="$(id -u):$(id -g)"
DOCKER_ARGS=(
    --rm
    --user "$RUN_USER"
    -e HOME=/tmp/verify-home
    -e USER=verify
    -v "${REGISTRY_ROOT}:/registry:ro"
)
if [[ -z "$COLD" ]]; then
    DOCKER_ARGS+=(-v "${CACHE_VOLUME}:/cache")
    # Named volumes are created root-owned; make sure the container user
    # can write to it.
    docker run --rm -v "${CACHE_VOLUME}:/cache" --entrypoint chown \
        --user root "$IMAGE" "$RUN_USER" /cache
fi
if [[ "${RBE:-}" == "1" ]]; then
    : "${RBE_CREDS:?RBE=1 requires RBE_CREDS=<dir containing bazelrc>}"
    DOCKER_ARGS+=(-e RBE=1 -v "${RBE_CREDS}:/rbe:ro")
fi

FAILED=
for BAZEL_VERSION in ${BAZEL_VERSIONS//,/ }; do
    VERSION_ARTIFACTS="${ARTIFACTS}/${MODULE}/${VERSION}/bazel-${BAZEL_VERSION}"
    mkdir -p "$VERSION_ARTIFACTS"
    echo ">>> Verifying ${MODULE}@${VERSION} with bazel ${BAZEL_VERSION}"
    if ! docker run \
             "${DOCKER_ARGS[@]}" \
             -v "${VERSION_ARTIFACTS}:/artifacts" \
             -e "USE_BAZEL_VERSION=${BAZEL_VERSION}" \
             "$IMAGE" \
             "$MODULE" "$VERSION"; then
        FAILED=1
    fi
done

[[ -z "$FAILED" ]]
