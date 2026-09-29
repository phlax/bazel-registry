# quiche 0.0.0-260922-7f07dc4.envoy

This module packages QUICHE from commit `7f07dc4d14c5702607a0dee9c7e4ab07f63f9883` (2026-09-22) with a generated root `BUILD.bazel` overlay, and a patch that removes upstream `*.bazel` files so the overlay is authoritative.

The overlay breaks the `@envoy` cycle by inlining lightweight compatibility macros and rewriting Envoy platform deps to `@quiche_deps` aliases configured by the `quiche.deps(...)` module extension. By default those aliases point at QUICHE's upstream default platform impl headers (or empty stubs for Envoy-only/test-only hooks). Envoy should override them to its real platform impl targets when consuming this module.

The overlay's `quiche_copts` carries the warning suppressions needed to build QUICHE cleanly under clang, so consumers need no `.bazelrc` flags for them.

The overlay also replaces Envoy's `config_setting`s: OS conditions resolve directly against `@platforms//os:*`, while `:apple`, `:windows_x86_64` and `:disable_http3` are defined locally in the overlay.

## `quiche.deps(...)` overrides

Configure Envoy's platform/library overrides from the root module instead of `.bazelrc` flags:

```starlark
quiche = use_extension("@quiche//:extensions.bzl", "quiche")
quiche.deps(
    mobile_quiche_bug_tracker_impl_lib = "@envoy//source/common/quic/platform:quiche_logging_impl_lib",
    quic_base_impl_lib = "@envoy//source/common/quic/platform:quic_base_impl_lib",
    quiche_expect_bug_impl_lib = "@envoy//test/common/quic/platform:quiche_expect_bug_impl_lib",
    quiche_export_impl_lib = "@envoy//source/common/quic/platform:quiche_export_impl_lib",
    quiche_flags_impl_lib = "@envoy//source/common/quic/platform:quiche_flags_impl_lib",
    quiche_logging_impl_lib = "@envoy//source/common/quic/platform:quiche_logging_impl_lib",
    quiche_lower_case_string_impl_lib = "@envoy//source/common/quic/platform:quiche_lower_case_string_impl_lib",
    quiche_platform_iovec_impl_lib = "@envoy//source/common/quic/platform:quiche_platform_iovec_impl_lib",
    quiche_stack_trace_impl_lib = "@envoy//source/common/quic/platform:quiche_stack_trace_impl_lib",
    quiche_test_helpers_impl_lib = "@envoy//test/common/quic/platform:quiche_test_helpers_impl_lib",
    quiche_test_impl_lib = "@envoy//test/common/quic/platform:quiche_test_impl_lib",
    quiche_test_output_impl_lib = "@envoy//test/common/quic/platform:quiche_test_output_impl_lib",
    quiche_thread_impl_lib = "@envoy//test/common/quic/platform:quiche_thread_impl_lib",
    quiche_time_utils_impl_lib = "@envoy//source/common/quic/platform:quiche_time_utils_impl_lib",
    ssl_lib = "@envoy//bazel:ssl",
    zlib = "@envoy//bazel:zlib",
)
```

The generated `@quiche_deps` aliases for `quiche_expect_bug_impl_lib`, `quiche_test_helpers_impl_lib`, `quiche_test_impl_lib`, `quiche_test_output_impl_lib`, and `quiche_thread_impl_lib` are marked `testonly = True`, so non-test QUICHE targets cannot accidentally pull Envoy's test-only implementations.

## Targets that cannot build standalone

The following targets **cannot build standalone** (i.e. without root-module-supplied platform impl overrides):

- **Any target that transitively links `quic_base_impl_lib`**: build/link will fail for targets that actually call into these abstractions. Envoy sets `quiche.deps(quic_base_impl_lib = ...)` in its `MODULE.bazel`.

- **Test targets depending on `quiche_test_impl_lib` / `quiche_test_helpers_impl_lib`**: these default to `empty_impl`. Any test that uses QUICHE's own test framework requires a real test impl to be injected.

- **Targets with a transitive dependency on `@googleurl`** (e.g. `quic_test_tools_test_utils_lib`, `quic_core_connection_lib`): the `googleurl` BCR module's BUILD uses a `cc_library` rule removed in Bazel 9+. These targets build correctly when using Bazel 8.x as required by the presubmit matrix.

## Test sources

The overlay defines no `cc_test` targets. Test sources are exposed as `filegroup`s (`:*_test_srcs`) for consumption by Envoy's own test targets in `//test/common/quic/quiche/BUILD`, which use the real `envoy_cc_test` macro and supply their own test main.
