SqInfo = provider(fields = ["sq", "runfiles"])


def _sq_toolchain_impl(ctx):
    runfiles = ctx.runfiles(files = [ctx.file.sq])
    return [
        platform_common.ToolchainInfo(
            sq_info = SqInfo(sq = ctx.file.sq, runfiles = runfiles),
        ),
    ]


sq_toolchain = rule(
    implementation = _sq_toolchain_impl,
    attrs = {
        "sq": attr.label(
            allow_single_file = True,
            cfg = "exec",
            executable = True,
            mandatory = True,
        ),
    },
)


def _current_sq_toolchain_impl(ctx):
    info = ctx.toolchains["//:toolchain_type"].sq_info
    return [
        DefaultInfo(
            executable = info.sq,
            files = depset([info.sq]),
            runfiles = info.runfiles,
        ),
    ]


current_sq_toolchain = rule(
    implementation = _current_sq_toolchain_impl,
    executable = True,
    toolchains = ["//:toolchain_type"],
)
