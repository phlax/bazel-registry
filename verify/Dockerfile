# Container for BCR-style module verification (CI tier 2).
#
# Derived from toolshed docker/bazel/Dockerfile with the following changes:
# - dropped the hardcoded security.ubuntu.com pool URL for libtinfo5 (no
#   longer required)
# - added libxml2 and libatomic1
# - bumped BAZELISK_VERSION (1.10.1 predates usable bzlmod) and pinned its
#   sha256 per-arch
# - runs under tini so signals propagate and the bazel server does not
#   outlive the container
# - no build-time uid/gid baking - credentials and uid are handled at
#   runtime (docker run --user) so images are potentially reusable
#
# Bazel version is left to runtime USE_BAZEL_VERSION so the image is
# version-agnostic.

FROM ubuntu:24.04@sha256:80dd3c3b9c6cecb9f1667e9290b3bc61b78c2678c02cbdae5f0fea92cc6734ab AS base

ARG APT_BASE_PKGS="\
    apt-transport-https \
    build-essential \
    ca-certificates \
    curl \
    git \
    language-pack-en-base \
    libatomic1 \
    libxml2 \
    patch \
    python3 \
    python3-yaml \
    tini \
    unzip \
    zip"
ENV LANGUAGE=en_US:en
ENV LANG=en_US.UTF-8
ENV LC_ALL=en_US.UTF-8
ENV DEBIAN_FRONTEND=noninteractive
RUN --mount=type=tmpfs,target=/var/cache/apt \
    --mount=type=tmpfs,target=/var/lib/apt/lists \
    <<EOF
set -e
apt-get update
apt-get install -y -qq --no-install-recommends $APT_BASE_PKGS
EOF
RUN <<EOF
set -e
echo 'Acquire::Languages {"none";};' > /etc/apt/apt.conf.d/60language
echo "LANG=\"${LANG}\"" > /etc/default/locale
echo "LANGUAGE=\"${LANGUAGE}\"" >> /etc/default/locale
locale-gen "$LANG"
update-locale "$LANG"
EOF


FROM base AS bazel
ARG BAZELISK_VERSION=1.29.0
ARG BAZELISK_SHA256_AMD64=5a408715e932c0250d28bd84555f12edbf70117de42f9181691c736eacc4a992
ARG BAZELISK_SHA256_ARM64=e20e8b0f4f240091b7a55bf17b9398bd4f40ee70ae0208dff95dd4c445fb4010
RUN <<EOF
set -e
ARCH="$(dpkg --print-architecture)"
case "$ARCH" in
    amd64) BAZELISK_SHA256="$BAZELISK_SHA256_AMD64" ;;
    arm64) BAZELISK_SHA256="$BAZELISK_SHA256_ARM64" ;;
    *) echo "Unsupported architecture: $ARCH" >&2; exit 1 ;;
esac
curl -fsSL --output \
    /usr/local/bin/bazel \
    "https://github.com/bazelbuild/bazelisk/releases/download/v${BAZELISK_VERSION}/bazelisk-linux-${ARCH}"
echo "${BAZELISK_SHA256}  /usr/local/bin/bazel" | sha256sum -c -
chmod +x /usr/local/bin/bazel
EOF


FROM bazel AS full
ARG APT_PKGS
RUN --mount=type=tmpfs,target=/var/cache/apt \
    --mount=type=tmpfs,target=/var/lib/apt/lists \
    <<EOF
set -e
if [ -n "$APT_PKGS" ]; then
    apt-get update
    apt-get install -y -qq --no-install-recommends $APT_PKGS
fi
EOF
ARG SETUP=:
COPY --chmod=755 \
    <<EOF /tmp/setup.sh
${SETUP}
EOF
RUN --mount=type=tmpfs,target=/var/cache/apt \
    --mount=type=tmpfs,target=/var/lib/apt/lists \
    <<EOF
/tmp/setup.sh
EXIT_CODE=$?
rm -f /tmp/setup.sh
exit $EXIT_CODE
EOF


FROM full
COPY --chmod=755 verify_module.py /usr/local/bin/verify_module.py
USER ubuntu
WORKDIR /home/ubuntu
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/verify_module.py"]
