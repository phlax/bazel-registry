#!/usr/bin/env python3
"""BCR-style module verification harness (CI tier 2).

Builds a changed module version against a scratch consumer workspace:

1. synthesise a consumer workspace with
   `bazel_dep(name=<module>, version=<version>)`
2. point it at the local registry via `--registry=file://...`,
   with BCR as fallback
3. build/test either the module's `test_module/` or an anonymous stub

The `presubmit.yml` schema (incl. `bcr_test_module`) is honoured so that
upstream modules can be run unmodified. Only `ubuntu*` tasks are
translated to the container; macos/windows/centos etc are ignored. A
module with *only* non-linux tasks skips with a note rather than passing
silently having run nothing.

This is expected to run inside the verification container (see the
adjacent Dockerfile) but only assumes bazel(isk), python3 + PyYAML,
curl/tar and a C++ toolchain.
"""

import argparse
import itertools
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

import yaml

BCR_URL = "https://bcr.bazel.build"
MATRIX_VAR_RE = re.compile(r"\$\{\{\s*(\w+)\s*\}\}")
SKIP_NOTE_EXIT = 0
WORKSPACE_TEST_MODULE = "test_module"
WORKSPACE_STUB = "stub"
NO_TEST_TARGETS_MSG = "No test targets were found"

# The Envoy CI worker image is used as *both* the local container that runs
# verify_module.py and the RBE exec platform.  They must be the same image
# so that compilation artefacts cached remotely are bit-for-bit compatible
# with a subsequent local repro.  Update both by changing this one constant.
ENVOY_CI_IMAGE = (
    "gcr.io/envoy-ci/envoy-build:worker-v0.1.8"
    "@sha256:934b50777b1eb9348b0e62cafd9eee5c79828e57d4ca08083c86d3099b14bb42"
)

# Minimal credential helper for EngFlow RBE.  Written into the consumer
# workspace at bazel/engflow-bazel-credential-helper.sh so that the
# --credential_helper flag in the generated .bazelrc can reference it via
# %workspace%.  The script follows the Bazel credential-helper protocol
# (https://github.com/bazelbuild/proposals/blob/main/designs/2022-06-07-bazel-credential-helpers.md):
# read a JSON request from stdin, write a JSON response to stdout.
# %s receives ${GITHUB_TOKEN} at run-time; printf is used so that the format
# string can be single-quoted (no bash escaping needed).
ENGFLOW_CREDENTIAL_HELPER = """\
#!/bin/bash
# EngFlow/mordenite credential helper.
# Emits Authorization: ****** for *.engflow.com endpoints.
# Called by Bazel for each remote-cache / remote-exec request.
# Protocol: read a JSON request from stdin, write a JSON response to stdout.
set -euo pipefail
cat > /dev/null
printf '{"headers":{"Authorization":["Bearer %s"]}}\\n' "${GITHUB_TOKEN}"
"""


class VerificationError(Exception):
    pass


def log(msg):
    print(f"[verify] {msg}", flush=True)


def expand_matrix(task, matrix):
    """Expand `${{ var }}` placeholders in a task against a matrix.

    Returns the list of concrete task dicts (cartesian product of the
    matrix variables actually referenced by the task).
    """
    text = yaml.safe_dump(task)
    referenced = sorted(set(MATRIX_VAR_RE.findall(text)) & set(matrix or {}))
    if not referenced:
        return [task]
    expanded = []
    for values in itertools.product(*(matrix[var] for var in referenced)):
        subs = dict(zip(referenced, values))

        def repl(match):
            return str(subs.get(match.group(1), match.group(0)))

        expanded.append(yaml.safe_load(MATRIX_VAR_RE.sub(repl, text)))
    return expanded


def linux_tasks(tasks, matrix):
    """Expand tasks and keep only those runnable in the ubuntu container."""
    runnable = []
    seen = set()
    for name, task in (tasks or {}).items():
        for concrete in expand_matrix(task, matrix):
            platform = concrete.get("platform", "")
            if platform and not str(platform).startswith("ubuntu"):
                continue
            # Platform (and bazel version) are container/runtime concerns
            # here - dedupe tasks that only differ by those.
            key = json.dumps(
                {k: v for k, v in sorted(concrete.items())
                 if k not in ("platform", "bazel", "name")},
                sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            runnable.append((name, concrete))
    return runnable


def parse_presubmit(presubmit_path):
    """Parse presubmit.yml -> (tasks, uses_test_module, module_path).

    Returns the linux-runnable concrete tasks. Raises VerificationError
    with a skip note if the presubmit declares tasks but none are
    runnable on linux.
    """
    if not presubmit_path.exists():
        return [], False, None
    config = yaml.safe_load(presubmit_path.read_text()) or {}
    tasks = linux_tasks(config.get("tasks"), config.get("matrix"))
    test_config = config.get("bcr_test_module") or {}
    test_tasks = linux_tasks(
        test_config.get("tasks"), test_config.get("matrix"))
    declared = bool(config.get("tasks")) or bool(test_config.get("tasks"))
    if declared and not tasks and not test_tasks:
        raise VerificationError(
            "SKIP: presubmit.yml declares only non-linux tasks; "
            "nothing to run in the ubuntu container")
    if test_tasks:
        return test_tasks, True, test_config.get("module_path")
    return tasks, False, None


def fetch_source(version_dir, dest):
    """Download and extract the module source, applying overlay/patches.

    Needed when presubmit.yml declares a `bcr_test_module` whose
    `module_path` points inside the source archive.
    """
    source = json.loads((version_dir / "source.json").read_text())
    archive = dest / "archive"
    archive.mkdir(parents=True)
    tarball = dest / "upstream.tar.gz"
    log(f"Fetching source archive: {source['url']}")
    subprocess.run(
        ["curl", "-fsSL", "-o", str(tarball), source["url"]], check=True)
    strip_prefix = source.get("strip_prefix")
    strip = (
        [f"--strip-components={len(pathlib.PurePosixPath(strip_prefix).parts)}"]
        if strip_prefix
        else [])
    subprocess.run(
        ["tar", "-xf", str(tarball), "-C", str(archive)] + strip, check=True)
    overlay_dir = version_dir / "overlay"
    if overlay_dir.is_dir():
        shutil.copytree(overlay_dir, archive, dirs_exist_ok=True)
    patches_dir = version_dir / "patches"
    if patches_dir.is_dir():
        for name in sorted(source.get("patches", {})):
            patch = patches_dir / name
            log(f"Applying patch: {name}")
            subprocess.run(
                ["patch", f"-p{source.get('patch_strip', 0)}", "-i",
                 str(patch)],
                cwd=archive, check=True)
    # The registry MODULE.bazel is authoritative (it may differ from the
    # archive's, eg via overlay already, but ensure it regardless).
    shutil.copy2(version_dir / "MODULE.bazel", archive / "MODULE.bazel")
    return archive


def create_workspace(args, version_dir, tasks, uses_test_module, module_path):
    """Create the scratch consumer workspace.

    Returns (workspace_path, workspace_kind).
    """
    workspace = pathlib.Path(args.scratch) / "workspace"
    workspace_kind = WORKSPACE_TEST_MODULE
    local_test_module = version_dir / "test_module"
    if local_test_module.is_dir():
        log(f"Using registry test_module: {local_test_module}")
        shutil.copytree(local_test_module, workspace)
    elif uses_test_module and module_path:
        log(f"Using bcr_test_module from source archive: {module_path}")
        archive = fetch_source(
            version_dir, pathlib.Path(args.scratch) / "source")
        test_module = archive / module_path
        if not test_module.is_dir():
            raise VerificationError(
                f"bcr_test_module.module_path not found in source archive: "
                f"{module_path}")
        shutil.copytree(test_module, workspace)
    else:
        log("No test module; synthesising anonymous stub consumer")
        workspace_kind = WORKSPACE_STUB
        workspace.mkdir(parents=True)
        (workspace / "MODULE.bazel").write_text(
            'module(name = "verify_consumer")\n'
            f'bazel_dep(name = "{args.module}", '
            f'version = "{args.version}")\n')
        (workspace / "BUILD.bazel").write_text("")
    # Pin the module version under test - a test module's own bazel_dep
    # may reference an older version.
    with (workspace / "MODULE.bazel").open("a") as f:
        f.write(
            f'\nsingle_version_override(module_name = "{args.module}", '
            f'version = "{args.version}")\n')
    write_bazelrc(args, workspace)
    return workspace, workspace_kind


def _write_rbe_config(workspace):
    """Write the EngFlow credential helper and return RBE bazelrc lines.

    The credential helper is written into the workspace so that the
    --credential_helper flag can reference it via %workspace%.

    --experimental_remote_downloader is deliberately NOT included.  This
    registry's purpose is asserting that fetched archives match source.json;
    routing module archive fetches through EngFlow's CAS would bypass exactly
    the property being verified.
    """
    cred_dir = workspace / "bazel"
    cred_dir.mkdir(exist_ok=True)
    cred_helper = cred_dir / "engflow-bazel-credential-helper.sh"
    cred_helper.write_text(ENGFLOW_CREDENTIAL_HELPER)
    cred_helper.chmod(0o755)
    # Client image and exec platform image must be the same so that cached
    # artefacts are bit-for-bit compatible between the two environments.
    container_image = f"docker://{ENVOY_CI_IMAGE}"
    return [
        "common:engflow-common --google_default_credentials=false",
        "common:engflow-common "
        "--credential_helper=*.engflow.com="
        "%workspace%/bazel/engflow-bazel-credential-helper.sh",
        "common:engflow-common --grpc_keepalive_time=60s",
        "common:engflow-common --grpc_keepalive_timeout=30s",
        "common:engflow-common --remote_cache_compression",
        "common:engflow-common --remote_retries=10",
        "common:engflow-common --remote_retry_max_delay=60s",
        "common:engflow-common "
        "--experimental_remote_cache_eviction_retries=5",
        "common:remote-cache "
        "--remote_cache=grpcs://mordenite.cluster.engflow.com",
        "common:remote-cache --remote_timeout=3600s",
        f"common:remote-exec "
        f"--remote_default_exec_properties=container-image={container_image}",
        "common:remote-exec "
        "--remote_executor=grpcs://mordenite.cluster.engflow.com",
        "common:remote-exec --jobs=200",
        "common:remote-exec --define=engflow_rbe=true",
        # Activate both configs.
        "build --config=engflow-common",
        "build --config=remote-cache",
        "build --config=remote-exec",
    ]


def write_bazelrc(args, workspace):
    """Write .bazelrc for the consumer workspace.

    RBE and local-only settings are mutually exclusive:
    - RBE off (default): disk cache + modest --jobs for local iteration.
    - RBE on (--rbe / RBE=1): no disk cache (two layers fighting is worse
      than one), remote cache + remote executor, --jobs=200.
    """
    registry_url = f"file://{pathlib.Path(args.registry).resolve()}"
    lines = [
        # Local registry first, BCR fallback.
        f"common --registry={registry_url}",
        f"common --registry={args.bcr}",
        "common --lockfile_mode=update",
        "build --verbose_failures",
        "build --show_timestamps",
    ]
    rbe = getattr(args, "rbe", False) or os.environ.get("RBE") == "1"
    if rbe:
        lines += _write_rbe_config(workspace)
    else:
        cache = pathlib.Path(args.cache) if args.cache else None
        if cache and os.access(cache, os.W_OK):
            lines += [
                f"common --disk_cache={cache / 'disk'}",
                f"common --repository_cache={cache / 'repository'}",
            ]
        lines.append("build --jobs=8")
    (workspace / ".bazelrc").write_text("\n".join(lines) + "\n")


def run_bazel(args, workspace, command, flags, targets, log_name,
              allow_no_test_targets=False):
    artifacts = pathlib.Path(args.artifacts)
    artifacts.mkdir(parents=True, exist_ok=True)
    build_log = artifacts / f"{log_name}.log"
    bep = artifacts / f"{log_name}.bep.json"
    cmd = [
        "bazel",
        # Ensure the bazel server does not outlive the run/container.
        "--max_idle_secs=15",
        command,
        f"--build_event_json_file={bep}",
    ] + flags + targets
    env = dict(os.environ, USE_BAZEL_VERSION=args.bazel)
    log(f"Running ({log_name}): USE_BAZEL_VERSION={args.bazel} "
        + " ".join(cmd))
    with build_log.open("w") as logfile:
        proc = subprocess.Popen(
            cmd, cwd=workspace, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for line in proc.stdout:
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()
            logfile.write(line.decode(errors="replace"))
        proc.wait()
    if proc.returncode != 0:
        if allow_no_test_targets and command == "test":
            log_text = build_log.read_text(errors="replace")
            if NO_TEST_TARGETS_MSG in log_text:
                log("No test targets found in test module; skipping tests")
                return
        raise VerificationError(
            f"bazel {command} failed (exit {proc.returncode}); "
            f"see {build_log} and {bep}")


def assert_local_registry(args, workspace):
    """Assert the module under test resolved from the local registry.

    A module present in both registries resolves to the first registry
    that has it - assert rather than assume. `bazel mod show_repo`
    reports the registry the module's MODULE.bazel was fetched from.
    """
    env = dict(os.environ, USE_BAZEL_VERSION=args.bazel)
    proc = subprocess.run(
        ["bazel", "--max_idle_secs=15", "mod", "show_repo", args.module],
        cwd=workspace, env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        raise VerificationError(
            f"bazel mod show_repo {args.module} failed:\n{proc.stderr}")
    registry_url = f"file://{pathlib.Path(args.registry).resolve()}"
    expected = f"{registry_url}/modules/{args.module}/{args.version}/MODULE.bazel"
    if expected not in proc.stdout:
        raise VerificationError(
            f"{args.module}@{args.version} did not resolve from the local "
            f"registry ({registry_url}):\n{proc.stdout}")
    log(f"✓ {args.module}@{args.version} resolved from local registry")


def default_flags_and_targets(args, tasks, workspace_kind):
    """Build/test flags+targets from presubmit tasks, or defaults.

    Returns (plans, allow_no_test_targets), where allow_no_test_targets is
    only enabled for the default test-module `bazel test //...` path.
    """
    if not tasks:
        if workspace_kind == WORKSPACE_TEST_MODULE:
            # Test-module workspaces should validate their own targets.
            return [([], ["//..."], [], ["//..."])], True
        return [([], [f"@{args.module}//..."], [], [])], False
    plans = []
    for name, task in tasks:
        plans.append((
            task.get("build_flags") or [],
            task.get("build_targets") or [],
            task.get("test_flags") or [],
            task.get("test_targets") or []))
    return plans, False


def verify(args):
    version_dir = (
        pathlib.Path(args.registry) / "modules" / args.module / args.version)
    if not version_dir.is_dir():
        raise VerificationError(f"No such module version: {version_dir}")
    tasks, uses_test_module, module_path = parse_presubmit(
        version_dir / "presubmit.yml")
    workspace, workspace_kind = create_workspace(
        args, version_dir, tasks, uses_test_module, module_path)
    assert_local_registry(args, workspace)
    plans, allow_no_test_targets = default_flags_and_targets(
        args, tasks, workspace_kind)
    for i, (build_flags, build_targets, test_flags, test_targets) \
            in enumerate(plans):
        suffix = f".{i}" if len(plans) > 1 else ""
        if build_targets:
            run_bazel(
                args, workspace, "build", build_flags, build_targets,
                f"build{suffix}")
        if test_targets:
            run_bazel(
                args, workspace, "test", test_flags, test_targets,
                f"test{suffix}", allow_no_test_targets=allow_no_test_targets)
        if not build_targets and not test_targets:
            run_bazel(
                args, workspace, "build", build_flags,
                [f"@{args.module}//..."], f"build{suffix}")
    log(f"✓ Verified {args.module}@{args.version} (bazel {args.bazel})")


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("module", help="Module name")
    parser.add_argument("version", help="Module version")
    parser.add_argument(
        "--registry", default="/registry",
        help="Path to the local registry (mounted read-only)")
    parser.add_argument(
        "--artifacts", default="/artifacts",
        help="Writable dir for build.log/BEP artifacts")
    parser.add_argument(
        "--cache", default="/cache",
        help="Disk/repository cache dir (ignored if not writable)")
    parser.add_argument(
        "--scratch", default=None,
        help="Scratch dir for the consumer workspace (default: mkdtemp)")
    parser.add_argument(
        "--bazel", default=os.environ.get("USE_BAZEL_VERSION", "8.x"),
        help="Bazel version (bazelisk USE_BAZEL_VERSION)")
    parser.add_argument(
        "--bcr", default=BCR_URL, help="Fallback registry URL")
    parser.add_argument(
        "--rbe", action="store_true",
        default=(os.environ.get("RBE") == "1"),
        help="Enable EngFlow RBE (remote cache + executor).  "
             "Also activated by RBE=1 in the environment.  "
             "Mutually exclusive with disk cache / local --jobs.")
    args = parser.parse_args(argv)
    if not args.scratch:
        args.scratch = tempfile.mkdtemp(prefix="verify-")
    return args


def main(argv=None):
    args = parse_args(argv)
    try:
        verify(args)
    except VerificationError as e:
        if str(e).startswith("SKIP"):
            log(str(e))
            return SKIP_NOTE_EXIT
        log(f"✗ {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
