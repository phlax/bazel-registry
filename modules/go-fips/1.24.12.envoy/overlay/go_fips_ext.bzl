"""Module extension providing the host-architecture FIPS Go toolchain."""

load("//:go_fips.bzl", "go_fips")

def _go_fips_ext_impl(_module_ctx):
    go_fips(name = "go_fips_host")

go_fips_ext = module_extension(
    implementation = _go_fips_ext_impl,
    doc = "Provides @go_fips_host, the Go toolchain BoringSSL's FIPS build runs.",
)
