# quiche 0.0.0-260810-90a1e22.envoy

This module packages QUICHE from commit `90a1e2218164586d4dc711bb9639a313d95de9df` (2026-08-10) with Envoy's `quiche.patch`, a generated root `BUILD.bazel` overlay, and a patch that removes upstream `*.bazel` files so the overlay is authoritative.

The overlay breaks the `@envoy` cycle by inlining lightweight compatibility macros and rewriting Envoy platform deps to `label_flag`s. By default those flags point at QUICHE's upstream default platform impl headers (or empty stubs for Envoy-only/test-only hooks). Envoy should override the public flags to its real platform impl targets when consuming this module.
