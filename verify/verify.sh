#!/usr/bin/env bash
# Local/CI runner for BCR-style module verification (CI tier 2).
#
# Uses the Envoy CI worker image (same image used as the EngFlow RBE exec
# platform) as the container.  The registry is bind-mounted read-only, the
# scratch consumer workspace stays container-internal, and artifacts
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
#   --image <ref>         Image ref (default: pinned ENVOY_CI_IMAGE from verify_module.py)
#   --rbe                 Enable EngFlow RBE (requires GITHUB_TOKEN in env)
#
# RBE is opt-in: pass --rbe (or set RBE=1).  Authentication uses GITHUB_TOKEN
# from the calling environment.  Nothing is baked into the image.

set -e -o pipefail

REGISTRY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Default image: the pinned Envoy CI worker image defined in verify_module.py.
# Both the local container and the RBE exec platform use this same image.
DEFAULT_IMAGE="$(python3 -c "
import sys
sys.path.insert(0, '${REGISTRY_ROOT}/verify')
import verify_module
print(verify_module.ENVOY_CI_IMAGE)
")"
IMAGE="${DEFAULT_IMAGE}"
CACHE_VOLUME=envoy-bazel-registry-verify-cache
ARTIFACTS="${PWD}/verify-artifacts"
BAZEL_VERSIONS="${BAZEL_VERSIONS:-8.x}"
COLD=
RBE="${RBE:-}"
ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --bazel) BAZEL_VERSIONS="$2"; shift 2 ;;
        --cold) COLD=1; shift ;;
        --artifacts) ARTIFACTS="$2"; shift 2 ;;
        --image) IMAGE="$2"; shift 2 ;;
        --rbe) RBE=1; shift ;;
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
    # Inject the verifier script from the registry checkout.
    -v "${REGISTRY_ROOT}/verify/verify_module.py:/usr/local/bin/verify_module.py:ro"
    --entrypoint python3
)
if [[ -z "$COLD" ]]; then
    DOCKER_ARGS+=(-v "${CACHE_VOLUME}:/cache")
    # Named volumes are created root-owned; make sure the container user
    # can write to it.
    docker run --rm -v "${CACHE_VOLUME}:/cache" --entrypoint chown \
        --user root "$IMAGE" "$RUN_USER" /cache
fi
if [[ "${RBE}" == "1" ]]; then
    : "${GITHUB_TOKEN:?--rbe / RBE=1 requires GITHUB_TOKEN to be set}"
    DOCKER_ARGS+=(-e RBE=1 -e GITHUB_TOKEN="${GITHUB_TOKEN}")
fi

FAILED=
for BAZEL_VERSION in ${BAZEL_VERSIONS//,/ }; do
    VERSION_ARTIFACTS="${ARTIFACTS}/${MODULE}/${VERSION}/bazel-${BAZEL_VERSION}"
    mkdir -p "$VERSION_ARTIFACTS"
    echo ">>> Verifying ${MODULE}@${VERSION} with bazel ${BAZEL_VERSION}"
    RBE_ARGS=()
    [[ "${RBE}" == "1" ]] && RBE_ARGS=(--rbe)
    if ! docker run \
             "${DOCKER_ARGS[@]}" \
             -v "${VERSION_ARTIFACTS}:/artifacts" \
             -e "USE_BAZEL_VERSION=${BAZEL_VERSION}" \
             "$IMAGE" \
             /usr/local/bin/verify_module.py \
             "$MODULE" "$VERSION" "${RBE_ARGS[@]}"; then
        FAILED=1
    fi
done

[[ -z "$FAILED" ]]
