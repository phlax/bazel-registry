# quiche 0.0.0-260810-90a1e22.envoy

This module packages QUICHE from commit `90a1e2218164586d4dc711bb9639a313d95de9df` (2026-08-10) with Envoy's `quiche.patch`, a generated root `BUILD.bazel` overlay, and a patch that removes upstream `*.bazel` files so the overlay is authoritative.

The overlay breaks the `@envoy` cycle by inlining lightweight compatibility macros and rewriting Envoy platform deps to `label_flag`s. By default those flags point at QUICHE's upstream default platform impl headers (or empty stubs for Envoy-only/test-only hooks). Envoy should override the public flags to its real platform impl targets when consuming this module.

## Label-flag overrides

| Flag | Default | Envoy override |
|------|---------|---------------|
| `//:quiche_logging_impl_lib` | upstream default logging impl | `//source/common/quic/platform:quiche_logging_impl_lib` |
| `//:quiche_flags_impl_lib` | upstream default flags impl | `//source/common/quic/platform:quiche_flags_impl_lib` |
| `//:quic_base_impl_lib` | empty stub | `//source/common/quic/platform:quic_base_impl_lib` |
| `//:quiche_mem_slice_impl_lib` | empty stub | `//source/common/quic/platform:quiche_mem_slice_impl_lib` |
| `//:quiche_time_utils_impl_lib` | upstream default time utils impl | `//source/common/quic/platform:quiche_time_utils_impl_lib` |
| `//:mobile_quiche_bug_tracker_impl_lib` | upstream default bug tracker impl | (mobile only) |

## Targets that cannot build standalone

The following targets **cannot build standalone** (i.e. without root-module-supplied platform impl overrides):

- **Any target that transitively links `quic_base_impl_lib` or `quiche_mem_slice_impl_lib`**: these flags default to empty stubs. Build/link will fail for targets that actually call into these abstractions. Envoy sets `--@quiche//:quic_base_impl_lib` and `--@quiche//:quiche_mem_slice_impl_lib` in its `.bazelrc`.

- **Test targets depending on `quiche_test_impl_lib` / `quiche_test_helpers_impl_lib`**: these default to `empty_impl`. Any test that uses QUICHE's own test framework requires a real test impl to be injected.

- **Targets with a transitive dependency on `@googleurl`** (e.g. `quic_test_tools_test_utils_lib`, `quic_core_connection_lib`): the `googleurl` BCR module's BUILD uses a `cc_library` rule removed in Bazel 9+. These targets build correctly when using Bazel 8.x as required by the presubmit matrix.

