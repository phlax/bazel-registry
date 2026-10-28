# bazel-registry

Envoy's Bazel module registry.

It holds bzlmod modules used by [Envoy](https://github.com/envoyproxy/envoy) and related Envoy projects when they are not available in the [Bazel Central Registry](https://registry.bazel.build/) or require Envoy-specific patches.

All versions in this registry carry an `.envoy` suffix to distinguish them from BCR versions.

## Usage

Add the registry to your `.bazelrc`, ahead of the BCR:

```
common --registry=https://raw.githubusercontent.com/envoyproxy/bazel-registry/main
common --registry=https://bcr.bazel.build
```

Then depend on modules as normal, for example:

```starlark
bazel_dep(name = "nghttp2", version = "1.66.0.envoy")
```

## Layout

Follows the standard Bazel registry layout: `bazel_registry.json` at the root and one directory per module under `modules/`, each containing `metadata.json` and one directory per version with `MODULE.bazel` and `source.json` plus optional `patches/` and `overlay/`.

## Publishing

The published baseline is the latest GitHub release tag for this repository, and every `{module, version}` present at that tag is treated as published.

Between publishes, each module may carry at most one unpublished version. If multiple unpublished versions accumulate for the same module, the prune workflow removes the older unpublished versions and keeps only the most recently appended version from that module's `metadata.json`.

## Immutability

Published module versions (those present in the latest GitHub release) cannot be modified or removed. Fixes must be published as a new version. Yank a version by adding it to `yanked_versions`; never un-yank it by deleting the entry.

## Automated registration

The **Register module** workflow accepts `workflow_dispatch` or a
`repository_dispatch` with event type `register-module` and `client_payload`:

```json
{"repository": "envoyproxy/examples", "ref": "v0.2.10", "version": "0.2.10", "dry-run": false}
```

Only `envoyproxy/*` repositories and plain `X.Y.Z` versions are accepted.
Optional `sha` (40 hex characters) verifies the ref's commit and pins upstream
file reads. Producer release workflows must send the dispatch separately.
Non-dry runs use the `ENVOY_CI_PUBLISH_APP_ID` and `ENVOY_CI_PUBLISH_APP_KEY`
secrets to open a PR as `publish-envoy[bot]`.

Source repositories must provide `.bcr/config.yml` with a `moduleRoots` list
(defaults to `["."]` when absent), and a `MODULE.bazel` at each root with literal
module name and release version. Each root needs
`.bcr/<root>/source.template.json` and `presubmit.yml`, plus
`metadata.template.json` for modules not yet in this registry. For root `.`,
these files live directly in `.bcr/`. Source templates contain `url`, `integrity`,
and `strip_prefix`, with `{OWNER}`, `{REPO}`, `{VERSION}`, and `{TAG}` placeholders;
`TAG` is the ref with one leading `v` removed. All modules must use the same
HTTPS release tarball. The generator calculates its integrity once and appends
the module root to `strip_prefix` when necessary.

Upstream `.bcr/<root>/patches/` and `overlay/` trees are copied and hashed
recursively; patches use `patch_strip: 1`. If the last metadata-listed registry
version has any patch/overlay filenames missing upstream, registration fails:
those fixes must first be upstreamed to the source repo's `.bcr/`.

New releases always use `<version>.envoy`; `.envoy.N` is reserved for manual
post-release registry-side fixes. The generator rewrites the module version and
same-release sibling dependencies, removes top-level local/git/archive overrides
and comment-only override blocks, and otherwise preserves `MODULE.bazel`.
Nonliteral names/versions or unsupported constructs fail rather than guess.
All dependencies are verified: `.envoy`/`.envoy.N` versions must exist locally
(including this run's entries), and other versions must exist in BCR.
Missing files, mismatched versions, duplicate registrations, lost patches, or
failed dependency checks abort before writing entries or opening a PR.

For local validation without modifying the registry:

```sh
python3 .github/workflows/register_module.py \
    --repository envoyproxy/examples --ref v0.2.10 --version 0.2.10 --dry-run
```

`--modules-root` selects a registry directory containing `modules/` (default `.`).
Dry-runs perform the same checks, including duplicate detection, but write no
registry files. Successful runs print a summary and expose modules, PR title,
branch, and summary through `GITHUB_OUTPUT` when set.
