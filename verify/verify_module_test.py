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


class WriteBazelrcTest(unittest.TestCase):
    """Tests for write_bazelrc() RBE/local-only mode switching."""

    def _make_args(self, rbe=False, cache=None, registry=None, bcr=None,
                   scratch=None):
        return types.SimpleNamespace(
            rbe=rbe,
            cache=cache or "",
            registry=registry or "/registry",
            bcr=bcr or verify_module.BCR_URL,
            scratch=scratch or "",
        )

    def _read_bazelrc(self, workspace):
        return (workspace / ".bazelrc").read_text()

    def test_rbe_off_writes_disk_cache_and_no_remote_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = pathlib.Path(tmp) / "ws"
            workspace.mkdir()
            cache_dir = pathlib.Path(tmp) / "cache"
            cache_dir.mkdir()
            args = self._make_args(rbe=False, cache=str(cache_dir))
            verify_module.write_bazelrc(args, workspace)
            rc = self._read_bazelrc(workspace)
            self.assertIn("--disk_cache=", rc)
            self.assertIn("--repository_cache=", rc)
            self.assertNotIn("--remote_cache", rc)
            self.assertNotIn("--remote_executor", rc)
            self.assertNotIn("remote-cache", rc)
            self.assertNotIn("remote-exec", rc)

    def test_rbe_off_writes_local_jobs_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = pathlib.Path(tmp) / "ws"
            workspace.mkdir()
            args = self._make_args(rbe=False)
            verify_module.write_bazelrc(args, workspace)
            rc = self._read_bazelrc(workspace)
            self.assertIn("--jobs=8", rc)
            self.assertNotIn("--jobs=200", rc)

    def test_rbe_on_writes_remote_cache_and_executor(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = pathlib.Path(tmp) / "ws"
            workspace.mkdir()
            args = self._make_args(rbe=True)
            verify_module.write_bazelrc(args, workspace)
            rc = self._read_bazelrc(workspace)
            self.assertIn("remote_cache=grpcs://mordenite.cluster.engflow.com",
                          rc)
            self.assertIn(
                "remote_executor=grpcs://mordenite.cluster.engflow.com", rc)
            self.assertIn("--jobs=200", rc)

    def test_rbe_on_does_not_write_disk_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = pathlib.Path(tmp) / "ws"
            workspace.mkdir()
            cache_dir = pathlib.Path(tmp) / "cache"
            cache_dir.mkdir()
            # Even with a writable cache dir, RBE mode must not add disk_cache.
            args = self._make_args(rbe=True, cache=str(cache_dir))
            verify_module.write_bazelrc(args, workspace)
            rc = self._read_bazelrc(workspace)
            self.assertNotIn("--disk_cache=", rc)
            self.assertNotIn("--repository_cache=", rc)

    def test_rbe_on_pins_exec_platform_to_envoy_ci_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = pathlib.Path(tmp) / "ws"
            workspace.mkdir()
            args = self._make_args(rbe=True)
            verify_module.write_bazelrc(args, workspace)
            rc = self._read_bazelrc(workspace)
            self.assertIn(verify_module.ENVOY_CI_IMAGE, rc)
            self.assertIn("container-image=docker://", rc)

    def test_rbe_on_writes_credential_helper_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = pathlib.Path(tmp) / "ws"
            workspace.mkdir()
            args = self._make_args(rbe=True)
            verify_module.write_bazelrc(args, workspace)
            cred_helper = workspace / "bazel" / "engflow-bazel-credential-helper.sh"
            self.assertTrue(cred_helper.exists(),
                            "credential helper script not written")
            self.assertTrue(cred_helper.stat().st_mode & 0o111,
                            "credential helper is not executable")

    def test_experimental_remote_downloader_never_appears(self):
        """Downloading must go through the normal fetch path, not EngFlow CAS.

        The registry's entire purpose is verifying that fetched archives
        match source.json; bypassing that via the remote downloader would
        defeat the verification.
        """
        with tempfile.TemporaryDirectory() as tmp:
            workspace = pathlib.Path(tmp) / "ws"
            workspace.mkdir()
            for rbe in (False, True):
                args = self._make_args(rbe=rbe)
                verify_module.write_bazelrc(args, workspace)
                rc = self._read_bazelrc(workspace)
                self.assertNotIn(
                    "experimental_remote_downloader", rc,
                    f"experimental_remote_downloader must never appear "
                    f"(rbe={rbe})")

    def test_rbe_env_var_activates_rbe_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = pathlib.Path(tmp) / "ws"
            workspace.mkdir()
            # args.rbe=False but RBE=1 in environment
            args = self._make_args(rbe=False)
            with mock.patch.dict("os.environ", {"RBE": "1"}):
                verify_module.write_bazelrc(args, workspace)
            rc = self._read_bazelrc(workspace)
            self.assertIn("remote_executor", rc)



if __name__ == '__main__':
    unittest.main()
