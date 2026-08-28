# quiche 0.0.0-260824-0140828.envoy

This module packages QUICHE from commit `01408281e0d4541113cd8c15185d70f30c773b36` (2026-08-24) with Envoy's `oghttp2_trailer_fix.patch`, a generated root `BUILD.bazel` overlay, and a patch that removes upstream `*.bazel` files so the overlay is authoritative.

The overlay breaks the `@envoy` cycle by inlining lightweight compatibility macros and rewriting Envoy platform deps to `label_flag`s. By default those flags point at QUICHE's upstream default platform impl headers (or empty stubs for Envoy-only/test-only hooks). Envoy should override the public flags to its real platform impl targets when consuming this module.

The overlay also replaces Envoy's `config_setting`s: OS conditions resolve directly against `@platforms//os:*`, while `:apple`, `:windows_x86_64` and `:disable_http3` are defined locally in the overlay.

## Label-flag overrides

Source platform impls, overridden by Envoy to `//source/common/quic/platform:<name>`:

| Flag | Default |
|------|---------|
| `//:quic_base_impl_lib` | `:quic_base_impl_lib_default` |
| `//:quiche_export_impl_lib` | `:quiche_export_impl_lib_default` |
| `//:quiche_flags_impl_lib` | `:quiche_flags_impl_lib_default` |
| `//:quiche_logging_impl_lib` | `:quiche_logging_impl_lib_default` |
| `//:quiche_lower_case_string_impl_lib` | `:quiche_lower_case_string_impl_lib_default` |
| `//:quiche_mem_slice_impl_lib` | `:quiche_mem_slice_impl_lib_default` |
| `//:quiche_platform_iovec_impl_lib` | `:quiche_platform_iovec_impl_lib_default` |
| `//:quiche_stack_trace_impl_lib` | `:quiche_stack_trace_impl_lib_default` |
| `//:quiche_time_utils_impl_lib` | `:quiche_time_utils_impl_lib_default` |
| `//:mobile_quiche_bug_tracker_impl_lib` | `:quiche_bug_tracker_impl_lib_default` (mobile only, selected on `@platforms//os:android` / `:ios`) |

Test platform impls, overridden by Envoy to `//test/common/quic/platform:<name>`:

| Flag | Default |
|------|---------|
| `//:quiche_expect_bug_impl_lib` | `:empty_impl` |
| `//:quiche_test_helpers_impl_lib` | `:empty_impl` |
| `//:quiche_test_impl_lib` | `:empty_impl` |
| `//:quiche_test_output_impl_lib` | `:empty_impl` |
| `//:quiche_thread_impl_lib` | `:empty_impl` |

Other:

| Flag | Default | Envoy override |
|------|---------|---------------|
| `//:zlib` | `@zlib//:z` | `//bazel:zlib` (allows selecting zlib-ng) |

## Targets that cannot build standalone

The following targets **cannot build standalone** (i.e. without root-module-supplied platform impl overrides):

- **Any target that transitively links `quic_base_impl_lib` or `quiche_mem_slice_impl_lib`**: build/link will fail for targets that actually call into these abstractions. Envoy sets `--@quiche//:quic_base_impl_lib` and `--@quiche//:quiche_mem_slice_impl_lib` in its `.bazelrc`.

- **Test targets depending on `quiche_test_impl_lib` / `quiche_test_helpers_impl_lib`**: these default to `empty_impl`. Any test that uses QUICHE's own test framework requires a real test impl to be injected.

- **Targets with a transitive dependency on `@googleurl`** (e.g. `quic_test_tools_test_utils_lib`, `quic_core_connection_lib`): the `googleurl` BCR module's BUILD uses a `cc_library` rule removed in Bazel 9+. These targets build correctly when using Bazel 8.x as required by the presubmit matrix.

## Test sources

The overlay defines no `cc_test` targets. Test sources are exposed as `filegroup`s (`:*_test_srcs`) for consumption by Envoy's own test targets in `//test/common/quic/quiche/BUILD`, which use the real `envoy_cc_test` macro and supply their own test main.
