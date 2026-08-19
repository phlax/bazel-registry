load("@rules_cc//cc:defs.bzl", "cc_library", "cc_test")

quiche_copts = [
    # hpack_huffman_decoder.cc overloads operator<<.
    "-Wno-unused-function",
    "-Wno-old-style-cast",
    # Envoy build should not fail if a dependency has a warning.
    "-Wno-error",
]

_EXTERNAL_DEPS = {
    "nghttp2": ["@nghttp2//:nghttp2"],
    "ssl": ["@boringssl//:ssl"],
}

def _expand_external_deps(external_deps):
    result = []
    for dep in external_deps:
        if dep not in _EXTERNAL_DEPS:
            fail("unsupported external_dep for bazel-registry quiche overlay: %s" % dep)
        result += _EXTERNAL_DEPS[dep]
    return result

def envoy_cc_library(name, srcs = [], hdrs = [], deps = [], visibility = ["//visibility:public"], defines = [], copts = [], external_deps = [], repository = None, tcmalloc_dep = None, hdrs_lib = None, stamped = None, **kwargs):
    cc_library(
        name = name,
        srcs = srcs,
        hdrs = hdrs,
        deps = deps + _expand_external_deps(external_deps),
        includes = ["."],
        visibility = visibility,
        defines = defines,
        copts = quiche_copts + copts,
        **kwargs
    )

def envoy_cc_test_library(name, srcs = [], hdrs = [], deps = [], visibility = ["//visibility:public"], defines = [], copts = [], external_deps = [], repository = None, tcmalloc_dep = None, hdrs_lib = None, stamped = None, **kwargs):
    cc_library(
        name = name,
        srcs = srcs,
        hdrs = hdrs,
        deps = deps + _expand_external_deps(external_deps),
        includes = ["."],
        visibility = visibility,
        defines = defines,
        copts = quiche_copts + copts,
        testonly = True,
        **kwargs
    )

def envoy_cc_test(name, srcs = [], deps = [], visibility = None, defines = [], copts = [], external_deps = [], repository = None, stamped = None, **kwargs):
    cc_test(
        name = name,
        srcs = srcs,
        deps = deps + _expand_external_deps(external_deps),
        includes = ["."],
        visibility = visibility,
        defines = defines,
        copts = quiche_copts + copts,
        **kwargs
    )

def envoy_quiche_platform_impl_cc_library(name, srcs = [], hdrs = [], deps = [], visibility = ["//visibility:public"], **kwargs):
    cc_library(
        name = name,
        srcs = srcs,
        hdrs = hdrs,
        deps = deps,
        includes = ["."],
        strip_include_prefix = "quiche/common/platform/default/",
        visibility = visibility,
        **kwargs
    )

def envoy_quiche_platform_impl_cc_test_library(name, srcs = [], hdrs = [], deps = [], visibility = ["//visibility:public"], **kwargs):
    cc_library(
        name = name,
        srcs = srcs,
        hdrs = hdrs,
        deps = deps,
        includes = ["."],
        strip_include_prefix = "quiche/common/platform/default/",
        visibility = visibility,
        testonly = True,
        **kwargs
    )

def envoy_quic_cc_library(name, srcs = [], hdrs = [], deps = [], visibility = ["//visibility:public"], defines = [], copts = [], external_deps = [], tags = [], repository = None, **kwargs):
    cc_library(
        name = name,
        srcs = srcs,
        hdrs = hdrs,
        deps = deps + _expand_external_deps(external_deps),
        includes = ["."],
        visibility = visibility,
        defines = defines,
        copts = quiche_copts + copts,
        tags = tags,
        **kwargs
    )

def envoy_quic_cc_test_library(name, srcs = [], hdrs = [], deps = [], visibility = ["//visibility:public"], defines = [], copts = [], external_deps = [], tags = [], repository = None, **kwargs):
    cc_library(
        name = name,
        srcs = srcs,
        hdrs = hdrs,
        deps = deps + _expand_external_deps(external_deps),
        includes = ["."],
        visibility = visibility,
        defines = defines,
        copts = quiche_copts + copts,
        tags = tags,
        testonly = True,
        **kwargs
    )

def envoy_quic_cc_test(name, srcs = [], deps = [], visibility = None, defines = [], copts = [], external_deps = [], tags = [], repository = None, **kwargs):
    cc_test(
        name = name,
        srcs = srcs,
        deps = deps + _expand_external_deps(external_deps),
        includes = ["."],
        visibility = visibility,
        defines = defines,
        copts = quiche_copts + copts,
        tags = tags,
        **kwargs
    )
