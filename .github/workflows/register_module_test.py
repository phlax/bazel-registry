#!/usr/bin/env python3
"""Tests for register_module.py; all network access is mocked."""

import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import register_module as register


VERSION = "1.2.3"
REPOSITORY = "envoyproxy/examples"
TEMPLATE = json.dumps({
    "integrity": "",
    "strip_prefix": "{REPO}-{VERSION}",
    "url": "https://github.com/{OWNER}/{REPO}/archive/refs/tags/v{TAG}.tar.gz",
})


class RegisterModuleTest(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.modules = self.root / "modules"
        self.real_read_url = register.read_url
        self.network = mock.patch.object(register, "read_url", side_effect=self.fetch)
        self.read_url = self.network.start()
        self.addCleanup(self.network.stop)
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict(os.environ, {"GITHUB_OUTPUT": ""}).start()
        self.data = {}
        self.raw = f"https://raw.githubusercontent.com/{REPOSITORY}/v{VERSION}/"
        self.api = f"https://api.github.com/repos/{REPOSITORY}/contents/"
        self.archive = f"https://github.com/{REPOSITORY}/archive/refs/tags/v{VERSION}.tar.gz"
        self.data[self.archive] = b"release archive"
        self.add_upstream(".", "examples")

    def fetch(self, url, optional=False, method="GET"):
        if url in self.data:
            return self.data[url]
        if optional:
            return None
        raise ValueError(f"{method} {url}: HTTP 404")

    def add_upstream(self, root, name, deps=""):
        prefix = "" if root == "." else root + "/"
        bcr = ".bcr/" + prefix
        self.data[self.raw + prefix + "MODULE.bazel"] = (
            f'module(name = "{name}", version = "{VERSION}")\n' + deps).encode()
        self.data[self.raw + bcr + "source.template.json"] = TEMPLATE.encode()
        self.data[self.raw + bcr + "presubmit.yml"] = b"# unchanged\r\nbuild_targets: []\r\n"
        self.data[self.raw + bcr + "metadata.template.json"] = json.dumps({
            "homepage": "https://example.com", "versions": ["old"], "yanked_versions": {"old": "bad"},
        }).encode()

    def add_existing(self, name="examples", versions=None, source=None):
        versions = versions or ["9.0.0.envoy", "0.1.0.envoy.1"]
        path = self.modules / name
        path.mkdir(parents=True)
        (path / "metadata.json").write_text(json.dumps({
            "versions": versions, "yanked_versions": {"9.0.0.envoy": "bad"}, "homepage": "keep",
        }))
        latest = path / versions[-1]
        latest.mkdir()
        (latest / "source.json").write_text(json.dumps(source or {}))
        return path

    def run_register(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return register.register(REPOSITORY, "v" + VERSION, VERSION, self.root, **kwargs)

    def test_module_rewrites_preserve_unrelated_bytes(self):
        original = '''# keep this header
module(
    name = "examples",
    version = '1.2.3',  # retain comment
)
bazel_dep(name = "sibling", version = "1.2.3", dev_dependency = True)
bazel_dep(name = "other", version = "1.2.3")
ext = use_extension("@other//:defs.bzl", "extension")
ext.configure(value = "local_path_override() # not code")
# local_path_override(
#     module_name = "envoy",
#     path = "../envoy",
# )
#
# local_path_override(
#     module_name = "envoy_api",
#     path = "../envoy/api",
# )

local_path_override(
    module_name = "sibling",
    path = "sibling",
)
git_override(module_name = "other", commit = "abc") # gone
archive_override(
    module_name = "archive",
    urls = ["https://example.com/(archive)"],
)
# keep this footer
'''
        expected = '''# keep this header
module(
    name = "examples",
    version = '1.2.3.envoy',  # retain comment
)
bazel_dep(name = "sibling", version = "1.2.3.envoy", dev_dependency = True)
bazel_dep(name = "other", version = "1.2.3")
ext = use_extension("@other//:defs.bzl", "extension")
ext.configure(value = "local_path_override() # not code")

# keep this footer
'''
        self.assertEqual(register.rewrite_module(original, VERSION, {"examples", "sibling"}), expected)

    def test_ordinary_comments_and_override_names_in_strings_preserved(self):
        text = '''module(name = "examples", version = "1.2.3")
# An explanation:
# local_path_override(module_name = "foo", path = ".")
x = "bazel_dep(name = 'unreal', version = '0')"
'''
        self.assertEqual(register.rewrite_module(text, VERSION, {"examples"}),
                         text.replace('version = "1.2.3"', 'version = "1.2.3.envoy"'))

    def test_dynamic_or_nested_calls_fail_not_guess(self):
        for text in (
                'module(name = "x", version = VERSION)',
                'module(name = "x", version = "1" + ".2.3")',
                'module(name = "x", version = "1.2.3")\n'
                'bazel_dep(name = "y", version = pick())',
                'if True:\n    local_path_override(module_name = "x", path = ".")',
                'x = [bazel_dep(name = "y", version = "1")]'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                register.rewrite_module(text, VERSION, {"x"})

    def test_config_list_forms_and_default(self):
        for config in ('moduleRoots: [".", filter-cc, wasm-cc]\n',
                       'moduleRoots:\n- "."\n- filter-cc\n- wasm-cc\nother: true\n',
                       "moduleRoots:\n  - '.' # root\n  - filter-cc\n  - wasm-cc\n"):
            self.assertEqual(register.module_roots(config), [".", "filter-cc", "wasm-cc"])
        self.assertEqual(register.module_roots("other: true\n"), ["."])
        for config in ("moduleRoots: []", "moduleRoots: [., .]",
                       "moduleRoots: [../escape]", "moduleRoots: not-a-list"):
            with self.subTest(config=config), self.assertRaises(ValueError):
                register.module_roots(config)

    def test_render_template_subroots(self):
        for root, strip in ((".", "examples-1.2.3"), ("filter-cc", "examples-1.2.3/filter-cc")):
            rendered = register.render_source(TEMPLATE, REPOSITORY, "v1.2.3", VERSION, root)
            self.assertEqual(rendered["strip_prefix"], strip)
            self.assertEqual(rendered["url"], self.archive)
            template = TEMPLATE.replace("{REPO}-{VERSION}", strip)
            self.assertEqual(register.render_source(
                template, REPOSITORY, "v1.2.3", VERSION, root)["strip_prefix"], strip)
        with self.assertRaisesRegex(ValueError, "Unresolved"):
            register.render_source(TEMPLATE.replace("{TAG}", "{UNKNOWN}"),
                                   REPOSITORY, "v1.2.3", VERSION, ".")

    def test_metadata_create_and_verbatim_presubmit(self):
        entries = self.run_register()
        path = self.modules / "examples"
        metadata = json.loads((path / "metadata.json").read_bytes())
        self.assertEqual(metadata["versions"], ["1.2.3.envoy"])
        self.assertEqual(metadata["yanked_versions"], {})
        self.assertEqual(metadata["homepage"], "https://example.com")
        for filename, data in entries["examples", "1.2.3.envoy"].items():
            self.assertEqual((path / "1.2.3.envoy" / filename).read_bytes(), data)
        self.assertEqual((path / "1.2.3.envoy/presubmit.yml").read_bytes(),
                         b"# unchanged\r\nbuild_targets: []\r\n")
        source = json.loads((path / "1.2.3.envoy/source.json").read_bytes())
        self.assertEqual(list(source), ["url", "integrity", "strip_prefix"])
        self.assertEqual(source["integrity"], register.integrity(b"release archive"))

    def test_metadata_append_and_duplicate_fails(self):
        path = self.add_existing()
        self.run_register()
        metadata = json.loads((path / "metadata.json").read_bytes())
        self.assertEqual(metadata["versions"], ["9.0.0.envoy", "0.1.0.envoy.1", "1.2.3.envoy"])
        self.assertEqual(metadata["homepage"], "keep")
        self.assertEqual(metadata["yanked_versions"], {"9.0.0.envoy": "bad"})
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.run_register()

    def test_dry_run_no_writes_and_outputs(self):
        output = self.root / "outputs"
        with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}):
            entries = self.run_register(dry_run=True)
        self.assertIn(("examples", "1.2.3.envoy"), entries)
        self.assertFalse(self.modules.exists())
        self.assertIn("modules=examples@1.2.3.envoy\n", output.read_text())
        self.assertIn("title=registry/examples: Add 1.2.3.envoy\n", output.read_text())
        self.assertIn("branch=register/examples/1.2.3\n", output.read_text())

    def test_multiple_modules_download_once_and_verify_siblings(self):
        self.data[self.raw + ".bcr/config.yml"] = b"moduleRoots: [., sibling]"
        self.add_upstream(".", "examples",
                          'bazel_dep(name = "sibling", version = "1.2.3")\n')
        self.add_upstream("sibling", "sibling")
        output = self.root / "outputs"
        with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}):
            self.run_register()
        self.assertEqual(sum(call.args[0] == self.archive
                             for call in self.read_url.call_args_list), 1)
        self.assertIn("title=registry/examples+: Add 1.2.3.envoy\n", output.read_text())
        self.assertEqual(json.loads((self.modules / "sibling/1.2.3.envoy/source.json").read_bytes())
                         ["strip_prefix"], "examples-1.2.3/sibling")
        self.assertIn('version = "1.2.3.envoy"', (
            self.modules / "examples/1.2.3.envoy/MODULE.bazel").read_text())

    def test_dependency_classification_and_collected_failures_no_writes(self):
        self.add_upstream(".", "examples", '''
bazel_dep(name = "missing", version = "1.0.envoy")
bazel_dep(name = "patched", version = "2.0.envoy.2")
bazel_dep(name = "bcr_missing", version = "3.0")
bazel_dep(name = "bcr_ok", version = "1.0")
bazel_dep(name = "envoyish", version = "1.0.envoyish")
''')
        self.data["https://bcr.bazel.build/modules/bcr_ok/1.0/MODULE.bazel"] = b""
        self.data["https://bcr.bazel.build/modules/envoyish/1.0.envoyish/MODULE.bazel"] = b""
        with self.assertRaises(ValueError) as raised:
            self.run_register()
        for missing in ("missing@1.0.envoy", "patched@2.0.envoy.2", "bcr_missing@3.0"):
            self.assertIn(missing, str(raised.exception))
        heads = [call.args[0] for call in self.read_url.call_args_list
                 if call.kwargs.get("method") == "HEAD"]
        self.assertEqual(len(heads), 3)
        self.assertFalse(self.modules.exists())

    def test_existing_registry_dependency(self):
        self.add_upstream(".", "examples", 'bazel_dep(name = "other", version = "2.0.envoy.1")')
        target = self.modules / "other/2.0.envoy.1/MODULE.bazel"
        target.parent.mkdir(parents=True)
        target.touch()
        self.run_register()
        self.assertFalse(any(c.kwargs.get("method") == "HEAD" for c in self.read_url.call_args_list))

    def test_patches_regression_uses_metadata_order(self):
        self.add_existing(source={"patches": {"fix.patch": "old"},
                                  "overlay": {"nested/BUILD.bazel": "old"}})
        with self.assertRaises(ValueError) as raised:
            self.run_register()
        for phrase in ("patches/fix.patch", "overlay/nested/BUILD.bazel",
                       "must be upstreamed to .bcr/"):
            self.assertIn(phrase, str(raised.exception))
        self.assertFalse((self.modules / "examples/1.2.3.envoy").exists())

    def test_patches_overlay_recursive_copy_hashes_and_guard(self):
        self.add_existing(source={"patches": {"fix.patch": "old"},
                                  "overlay": {"nested/BUILD.bazel": "old"}})
        for path, listing in (
                ("patches", [{"name": "fix.patch", "type": "file"}]),
                ("overlay", [{"name": "nested", "type": "dir"}]),
                ("overlay/nested", [{"name": "BUILD.bazel", "type": "file"}])):
            self.data[self.api + ".bcr/" + path + "?ref=v1.2.3"] = json.dumps(listing).encode()
        self.data[self.raw + ".bcr/patches/fix.patch"] = b"patch bytes\n"
        self.data[self.raw + ".bcr/overlay/nested/BUILD.bazel"] = b"overlay bytes\n"
        self.run_register()
        entry = self.modules / "examples/1.2.3.envoy"
        source = json.loads((entry / "source.json").read_bytes())
        self.assertEqual(list(source),
                         ["url", "integrity", "strip_prefix", "patch_strip", "patches", "overlay"])
        self.assertEqual(source["patch_strip"], 1)
        for kind, path in (("patches", "fix.patch"), ("overlay", "nested/BUILD.bazel")):
            data = self.data[self.raw + ".bcr/" + kind + "/" + path]
            self.assertEqual((entry / kind / path).read_bytes(), data)
            self.assertEqual(source[kind][path], register.integrity(data))

    def test_invalid_inputs_fail_before_network(self):
        for repository, ref, version, sha in (
                ("other/examples", "v1", VERSION, ""),
                (REPOSITORY, "", VERSION, ""),
                (REPOSITORY, "v1\ninjection", VERSION, ""),
                (REPOSITORY, "v1", "1.2.3-dev", ""),
                (REPOSITORY, "v1", VERSION, "bad")):
            with self.assertRaises(ValueError):
                register.register(repository, ref, version, self.root, sha=sha)
        self.read_url.assert_not_called()

    def test_upstream_version_mismatch_and_missing_presubmit(self):
        self.data[self.raw + "MODULE.bazel"] = b'module(name = "examples", version = "9.0.0")'
        with self.assertRaisesRegex(ValueError, "upstream version"):
            self.run_register()
        self.add_upstream(".", "examples")
        del self.data[self.raw + ".bcr/presubmit.yml"]
        with self.assertRaisesRegex(ValueError, "presubmit.yml"):
            self.run_register()
        self.assertFalse(self.modules.exists())

    def test_optional_sha_is_verified_and_pins_upstream_reads(self):
        sha = "a" * 40
        self.data[f"https://api.github.com/repos/{REPOSITORY}/commits/v1.2.3"] = (
            json.dumps({"sha": sha}).encode())
        self.data.update({url.replace("/v1.2.3/", "/" + sha + "/"): data
                          for url, data in list(self.data.items()) if url.startswith(self.raw)})
        self.run_register(sha=sha, dry_run=True)
        with self.assertRaisesRegex(ValueError, "does not resolve"):
            self.run_register(sha="b" * 40, dry_run=True)

    def test_read_url_only_404_is_optional(self):
        with mock.patch.object(register.urllib.request, "urlopen") as urlopen:
            for code in (404, 403, 500):
                urlopen.side_effect = urllib.error.HTTPError(
                    "https://example.com", code, "failure", {}, None)
                if code == 404:
                    self.assertIsNone(self.real_read_url("https://example.com", optional=True))
                else:
                    with self.assertRaisesRegex(ValueError, f"HTTP {code}"):
                        self.real_read_url("https://example.com", optional=True)


if __name__ == "__main__":
    unittest.main()
