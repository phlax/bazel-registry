# Module verification (CI tier 2)

Containerised BCR-style verification of registry module versions: each
module version is built against a scratch consumer workspace, resolved
from this registry first with the BCR as fallback.

The harness is the artifact; CI is a thin caller. It runs identically
locally and in CI.

## Usage

```console
$ verify/verify.sh <module> <version>

# eg
$ verify/verify.sh liburing 2.15.envoy

# force the cold path (no cache volume - this is what CI tests)
$ verify/verify.sh --cold liburing 2.15.envoy

# specific bazel version(s)
$ verify/verify.sh --bazel 8.x liburing 2.15.envoy
```

Artifacts (`build.log` and `--build_event_json_file` BEP - the BEP is
what makes a failed remote build diagnosable) land in
`./verify-artifacts/<module>/<version>/bazel-<version>/`.

## What gets run

- `test_module/` present in the registry version dir → build/test it
- `presubmit.yml` present → its `ubuntu*` tasks are translated to the
  container (`build_targets`/`test_targets`/`build_flags`/`test_flags`,
  incl. `bcr_test_module`); macos/windows/centos tasks are ignored. A
  module with *only* non-linux tasks skips with a note.
- otherwise → an anonymous stub consumer is synthesised and
  `bazel build @<module>//...` run

After the build, the harness *asserts* the module version resolved from
the local registry (`bazel mod show_repo`) rather than the BCR.

## Layout

- `Dockerfile` - ubuntu 24.04 + bazelisk (sha256-pinned), derived from
  toolshed `docker/bazel`. Bazel version is left to runtime
  `USE_BAZEL_VERSION` so the image is version-agnostic. Runs under
  `tini` so the bazel server does not outlive the container. uid/gid
  and credentials are runtime concerns - nothing user- or
  credential-specific is baked into the image.
- `verify_module.py` - the harness; runs inside the container. The
  registry is bind-mounted read-only at `/registry`, artifacts are the
  only writable mount (`/artifacts`), the scratch consumer workspace
  stays container-internal and the disk/repository cache sits on a
  named volume at `/cache`.
- `verify.sh` - local/CI runner; builds the image and wires up the
  mounts.

## RBE

Opt-in and off by default so a fresh clone works with zero setup:

```console
$ RBE=1 RBE_CREDS=/path/to/creds verify/verify.sh <module> <version>
```

`RBE_CREDS` is bind-mounted read-only at `/rbe` and must contain a
`bazelrc` snippet with the remote config; nothing is baked into the
image.
