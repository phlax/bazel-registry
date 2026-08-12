#!/usr/bin/env python3

import types
import unittest
from unittest import mock
import pathlib
import tempfile

import verify_module


class DefaultFlagsAndTargetsTest(unittest.TestCase):

    def test_test_module_workspace_without_presubmit_defaults_to_build_and_test_workspace_targets(self):
        args = types.SimpleNamespace(module="liburing")
        self.assertEqual(
            verify_module.default_flags_and_targets(
                args, [], verify_module.WORKSPACE_TEST_MODULE),
            ([([], ["//..."], [], ["//..."])], True),
        )

    def test_stub_workspace_without_presubmit_defaults_to_build_module_targets_only(self):
        args = types.SimpleNamespace(module="liburing")
        self.assertEqual(
            verify_module.default_flags_and_targets(
                args, [], verify_module.WORKSPACE_STUB),
            ([([], ["@liburing//..."], [], [])], False),
        )

    def test_explicit_presubmit_targets_take_precedence_for_test_module_workspace(self):
        args = types.SimpleNamespace(module="liburing")
        tasks = [("ci", {
            "build_flags": ["--config=dbg"],
            "build_targets": ["//:build_me"],
            "test_flags": ["--test_output=errors"],
            "test_targets": ["//:test_me"],
        })]
        self.assertEqual(
            verify_module.default_flags_and_targets(
                args, tasks, verify_module.WORKSPACE_TEST_MODULE),
            ([(
                ["--config=dbg"],
                ["//:build_me"],
                ["--test_output=errors"],
                ["//:test_me"],
            )], False),
        )

    def test_explicit_presubmit_targets_take_precedence_for_stub_workspace(self):
        args = types.SimpleNamespace(module="liburing")
        tasks = [("ci", {
            "build_targets": ["@liburing//:liburing"],
            "test_targets": ["@liburing//:liburing_test"],
        })]
        self.assertEqual(
            verify_module.default_flags_and_targets(
                args, tasks, verify_module.WORKSPACE_STUB),
            ([([], ["@liburing//:liburing"], [], ["@liburing//:liburing_test"])], False),
        )


class VerifyOrderTest(unittest.TestCase):

    def test_verify_asserts_local_registry_before_build_or_test(self):
        with tempfile.TemporaryDirectory() as registry_root:
            version_dir = pathlib.Path(
                registry_root) / "modules" / "liburing" / "2.15.envoy"
            version_dir.mkdir(parents=True)
            args = types.SimpleNamespace(
                registry=registry_root,
                module="liburing",
                version="2.15.envoy",
                bazel="8.x",
            )
            order = []
            commands = []

            def record_run_bazel(_args, _workspace, command, *_rest, **_kwargs):
                order.append("run_bazel")
                commands.append(command)

            with mock.patch.object(
                verify_module, "parse_presubmit",
                return_value=([], False, None),
            ), mock.patch.object(
                verify_module, "create_workspace",
                return_value=(pathlib.Path(registry_root), verify_module.WORKSPACE_TEST_MODULE),
            ), mock.patch.object(
                verify_module, "default_flags_and_targets",
                return_value=([([], ["//..."], [], ["//..."])], True),
            ), mock.patch.object(
                verify_module, "assert_local_registry",
                side_effect=lambda *_: order.append("assert_local_registry"),
            ), mock.patch.object(
                verify_module, "run_bazel",
                side_effect=record_run_bazel,
            ):
                verify_module.verify(args)

            self.assertEqual(order[0], "assert_local_registry")
            self.assertEqual(commands, ["build", "test"])


if __name__ == "__main__":
    unittest.main()
