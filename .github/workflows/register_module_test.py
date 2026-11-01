#!/usr/bin/env python3
"""Tests for register_module.py; all network access is mocked."""

import contextlib
import gzip
import io
import json
import os
import pathlib
import sys
import tarfile
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
        # Archives built from the upstream files at fetch time: {url: prefix}.
        self.archives = {self.archive: f"examples-{VERSION}"}
        self.add_upstream(".", "examples")

    def build_archive(self, prefix):
        """A deterministic tar.gz of the upstream MODULE.bazel files under `prefix`."""
        buffer = io.BytesIO()
        with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as archive:
                for url, data in sorted(self.data.items()):
                    if url.startswith(self.raw) and url.endswith("MODULE.bazel"):
                        info = tarfile.TarInfo(prefix + "/" + url[len(self.raw):])
                        info.size = len(data)
                        archive.addfile(info, io.BytesIO(data))
        return buffer.getvalue()

    def fetch(self, url, optional=False, method="GET"):
        if url in self.archives:
            return self.build_archive(self.archives[url])
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
        # Templates give the full strip_prefix, including any subdirectory.
        self.data[self.raw + bcr + "source.template.json"] = (
            TEMPLATE if root == "." else TEMPLATE.replace("{REPO}-{VERSION}",
                                                          "{REPO}-{VERSION}/" + root)).encode()
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
            return register.register(REPOSITORY, "v" + VERSION, self.root, **kwargs)

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
        self.assertEqual(register.rewrite_module(original, VERSION, {"sibling": VERSION}), expected)

    def test_ordinary_comments_and_override_names_in_strings_preserved(self):
        text = '''module(name = "examples", version = "1.2.3")
# An explanation:
# local_path_override(module_name = "foo", path = ".")
x = "bazel_dep(name = 'unreal', version = '0')"
'''
        self.assertEqual(register.rewrite_module(text, VERSION, {}),
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
                register.rewrite_module(text, VERSION, {"x": VERSION})

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

    def test_render_template_uses_strip_prefix_as_given(self):
        for strip in ("examples-1.2.3", "examples-1.2.3/filter-cc", "toolshed-jq-v1.2.3"):
            template = TEMPLATE.replace("{REPO}-{VERSION}", strip)
            rendered = register.render_source(template, REPOSITORY, "v1.2.3", VERSION)
            self.assertEqual(rendered["strip_prefix"], strip)
            self.assertEqual(rendered["url"], self.archive)
        with self.assertRaisesRegex(ValueError, "Unresolved"):
            register.render_source(TEMPLATE.replace("{TAG}", "{UNKNOWN}"),
                                   REPOSITORY, "v1.2.3", VERSION)

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
        self.assertEqual(source["integrity"], register.integrity(self.fetch(self.archive)))

    def test_metadata_append_and_rerun_skips(self):
        path = self.add_existing()
        self.run_register()
        metadata = json.loads((path / "metadata.json").read_bytes())
        self.assertEqual(metadata["versions"], ["9.0.0.envoy", "0.1.0.envoy.1", "1.2.3.envoy"])
        self.assertEqual(metadata["homepage"], "keep")
        self.assertEqual(metadata["yanked_versions"], {"9.0.0.envoy": "bad"})
        output = self.root / "outputs"
        with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}):
            self.assertEqual(self.run_register(), {})
        self.assertIn("registered=false\n", output.read_text())
        self.assertIn("examples@1.2.3.envoy: already registered", output.read_text())

    def test_dry_run_no_writes_and_outputs(self):
        output = self.root / "outputs"
        with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}):
            entries = self.run_register(dry_run=True)
        self.assertIn(("examples", "1.2.3.envoy"), entries)
        self.assertFalse(self.modules.exists())
        self.assertIn("registered=true\n", output.read_text())
        self.assertIn("modules=examples@1.2.3.envoy\n", output.read_text())
        self.assertIn("title=registry/examples: Add 1.2.3.envoy\n", output.read_text())
        self.assertIn("branch=register/examples/examples-1.2.3.envoy\n", output.read_text())

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
        for repository, ref, sha in (
                ("other/examples", "v1", ""),
                (REPOSITORY, "", ""),
                (REPOSITORY, "v1\ninjection", ""),
                (REPOSITORY, "v1.2.3'$(id)'", ""),
                (REPOSITORY, "v1.2.3`id`", ""),
                (REPOSITORY, "v1.2.3\\", ""),
                (REPOSITORY, "v1", "bad")):
            with self.assertRaises(ValueError):
                register.register(repository, ref, self.root, sha=sha)
        self.read_url.assert_not_called()

    def test_presubmit_is_optional(self):
        del self.data[self.raw + ".bcr/presubmit.yml"]
        entries = self.run_register()
        self.assertNotIn("presubmit.yml", entries["examples", "1.2.3.envoy"])
        self.assertFalse((self.modules / "examples/1.2.3.envoy/presubmit.yml").exists())

    def test_archive_module_must_match_ref(self):
        archive = self.fetch(self.archive)
        self.archives.clear()
        self.data[self.archive] = archive
        self.data[self.raw + "MODULE.bazel"] += b"# changed after the release\n"
        with self.assertRaisesRegex(ValueError, "differs from"):
            self.run_register()
        self.data[self.archive] = b"not an archive"
        with self.assertRaisesRegex(ValueError, "Unsupported release archive"):
            self.run_register()
        self.assertFalse(self.modules.exists())

    def test_dev_versions_skipped_and_nothing_to_register(self):
        self.data[self.raw + "MODULE.bazel"] = b'module(name = "examples", version = "1.2.4-dev")\n'
        output = self.root / "outputs"
        with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}):
            self.assertEqual(self.run_register(), {})
        self.assertIn("registered=false\n", output.read_text())
        self.assertIn("examples@1.2.4-dev: not a release version", output.read_text())
        self.assertNotIn("branch=", output.read_text())
        self.assertFalse(self.modules.exists())

    def test_registry_templates_preferred_over_bcr(self):
        # `.envoy-registry/` overrides `.bcr/` (eg kept for BCR publishing).
        self.data[self.raw + ".bcr/source.template.json"] = b'{"broken": true}'
        self.data[self.raw + ".envoy-registry/config.yml"] = b"moduleRoots: [.]"
        self.data[self.raw + ".envoy-registry/source.template.json"] = TEMPLATE.encode()
        self.data[self.raw + ".envoy-registry/metadata.template.json"] = b'{"versions": []}'
        entries = self.run_register()
        self.assertIn(("examples", "1.2.3.envoy"), entries)
        self.assertNotIn("presubmit.yml", entries["examples", "1.2.3.envoy"])

    def test_independently_versioned_roots(self):
        # A monorepo ref where each root has its own release and tarball: only
        # the new releases register, each hashed from its own archive, and a
        # dependency on an already-registered sibling still becomes `.envoy`.
        self.data[self.raw + ".bcr/config.yml"] = b"moduleRoots: [bazel, jq, rules_jq]"
        roots = {"bazel": ("envoy_toolshed", VERSION), "jq": ("envoy_toolshed_jq", VERSION),
                 "rules_jq": ("rules_jq", "0.0.2-dev")}
        for root, (name, version) in roots.items():
            url = f"https://github.com/{REPOSITORY}/releases/download/v{{TAG}}/{root}.tar.gz"
            self.data[self.raw + root + "/MODULE.bazel"] = (
                f'module(name = "{name}", version = "{version}")\n'
                + ('bazel_dep(name = "envoy_toolshed_jq", version = "1.2.3")\n'
                   'bazel_dep(name = "rules_jq", version = "0.0.0", dev_dependency = True)\n'
                   if root == "bazel" else "")).encode()
            self.data[self.raw + ".bcr/" + root + "/source.template.json"] = json.dumps({
                "url": url, "integrity": "", "strip_prefix": f"{root}-v{{VERSION}}/{root}",
            }).encode()
            self.data[self.raw + ".bcr/" + root + "/metadata.template.json"] = b'{"versions": []}'
            self.archives[url.replace("{TAG}", VERSION)] = f"{root}-v{version}"
        (self.add_existing("envoy_toolshed_jq", versions=["1.2.3.envoy"])
         / "1.2.3.envoy/MODULE.bazel").touch()
        entries = self.run_register()
        self.assertEqual(list(entries), [("envoy_toolshed", "1.2.3.envoy")])
        source = json.loads((self.modules / "envoy_toolshed/1.2.3.envoy/source.json").read_bytes())
        self.assertEqual(source["strip_prefix"], "bazel-v1.2.3/bazel")
        self.assertEqual(source["integrity"], register.integrity(self.fetch(source["url"])))
        module = (self.modules / "envoy_toolshed/1.2.3.envoy/MODULE.bazel").read_text()
        self.assertIn('bazel_dep(name = "envoy_toolshed_jq", version = "1.2.3.envoy")', module)
        # The dev dependency is neither rewritten nor verified.
        self.assertIn('bazel_dep(name = "rules_jq", version = "0.0.0", dev_dependency = True)', module)
        self.assertFalse(any(c.kwargs.get("method") == "HEAD" for c in self.read_url.call_args_list))

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
