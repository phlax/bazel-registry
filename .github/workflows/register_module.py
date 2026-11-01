#!/usr/bin/env python3
"""Register an upstream release using its .bcr templates (stdlib only)."""

import argparse
import ast
import base64
import hashlib
import io
import json
import os
import pathlib
import re
import sys
import tarfile
import tokenize
import urllib.error
import urllib.parse
import urllib.request
import zipfile


OVERRIDES = {"local_path_override", "git_override", "archive_override"}
CALLS = OVERRIDES | {"module", "bazel_dep"}
# Upstream template directory for this registry; `.bcr/` is the fallback.
TEMPLATES = ".envoy-registry"
# Only plain release versions are registered; anything else (eg `-dev`) is skipped.
RELEASE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")


def validate_inputs(repository, ref, sha=""):
    if not re.fullmatch(r"envoyproxy/[A-Za-z0-9_.-]+", repository):
        raise ValueError("repository must match envoyproxy/[A-Za-z0-9_.-]+")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/+-]*", ref):
        raise ValueError("ref must use only letters, digits, dot, underscore, slash, plus or hyphen")
    if sha and not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
        raise ValueError("sha must be 40 hexadecimal characters")


def safe_path(value, root=False):
    if root and value == ".":
        return value
    if (not isinstance(value, str) or not value
            or any(not re.fullmatch(r"[A-Za-z0-9_.+-]+", p)
                   or p in (".", "..") for p in value.split("/"))):
        raise ValueError(f"Unsafe relative path: {value!r}")
    return value


def module_roots(config):
    """Read only the moduleRoots YAML list; reject unsupported list syntax."""
    lines = config.splitlines()
    for index, line in enumerate(lines):
        if not re.match(r"^moduleRoots\s*:", line):
            continue
        value = line.split(":", 1)[1].split("#", 1)[0].strip()
        if value:
            if not (value.startswith("[") and value.endswith("]")):
                raise ValueError("moduleRoots must be a YAML list")
            items = value[1:-1].split(",") if value[1:-1].strip() else []
        else:
            items = []
            for following in lines[index + 1:]:
                following = following.split("#", 1)[0].rstrip()
                if not following.strip():
                    continue
                match = re.fullmatch(r"\s*-\s+(.+)", following)
                if match:
                    items.append(match[1])
                elif following[0].isspace():
                    raise ValueError("Unsupported moduleRoots YAML syntax")
                else:
                    break
        roots = [safe_path(item.strip().strip("\"'"), root=True) for item in items]
        if not roots or len(set(roots)) != len(roots):
            raise ValueError("moduleRoots must be nonempty and unique")
        return roots
    return ["."]


def calls(text):
    """Locate relevant top-level calls without interpreting Starlark."""
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))

    def offset(pos):
        return offsets[pos[0] - 1] + pos[1]

    ignored = {tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE,
               tokenize.ENCODING, tokenize.ENDMARKER}
    tokens = [t for t in tokenize.generate_tokens(io.StringIO(text).readline)
              if t.type not in ignored]
    result = []
    depth = 0
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.type == tokenize.NAME and token.string in CALLS:
            if depth or token.start[1] or index + 1 == len(tokens) or tokens[index + 1].string != "(":
                raise ValueError(f"Unsupported non-top-level {token.string} at line {token.start[0]}")
            end = index + 2
            nesting = 1
            while end < len(tokens) and nesting:
                if tokens[end].string in ("(", "[", "{"):
                    nesting += 1
                elif tokens[end].string in (")", "]", "}"):
                    nesting -= 1
                end += 1
            if nesting:
                raise ValueError(f"Unclosed {token.string}")
            arguments = tokens[index + 2:end - 1]
            attrs = {}
            nesting = 0
            for n, arg in enumerate(arguments):
                if (not nesting and arg.type == tokenize.NAME
                        and n + 1 < len(arguments) and arguments[n + 1].string == "="):
                    stop = n + 2
                    nested = 0
                    while stop < len(arguments):
                        part = arguments[stop].string
                        if part == "," and not nested:
                            break
                        if part in ("(", "[", "{"):
                            nested += 1
                        elif part in (")", "]", "}"):
                            nested -= 1
                        stop += 1
                    value_tokens = arguments[n + 2:stop]
                    if arg.string == "dev_dependency":
                        if (len(value_tokens) != 1
                                or value_tokens[0].string not in ("True", "False")):
                            raise ValueError(f"{token.string}.dev_dependency must be True or False")
                        attrs["dev_dependency"] = value_tokens[0].string == "True"
                    elif arg.string in ("name", "version"):
                        if len(value_tokens) != 1 or value_tokens[0].type != tokenize.STRING:
                            raise ValueError(f"{token.string}.{arg.string} must be a string literal")
                        value_token = value_tokens[0]
                        value = ast.literal_eval(value_token.string)
                        if not isinstance(value, str) or arg.string in attrs:
                            raise ValueError(f"Invalid {token.string}.{arg.string}")
                        attrs[arg.string] = (value, offset(value_token.start), offset(value_token.end))
                if arg.string in ("(", "[", "{"):
                    nesting += 1
                elif arg.string in (")", "]", "}"):
                    nesting -= 1
            result.append((token.string, offset(token.start), offset(tokens[end - 1].end), attrs))
            index = end
            continue
        if token.string in ("(", "[", "{"):
            depth += 1
        elif token.string in (")", "]", "}"):
            depth -= 1
        index += 1
    return result


def identity(text):
    modules = [attrs for name, _, _, attrs in calls(text) if name == "module"]
    if len(modules) != 1 or not {"name", "version"} <= modules[0].keys():
        raise ValueError("MODULE.bazel must contain one module with literal name and version")
    name, version = modules[0]["name"][0], modules[0]["version"][0]
    if not re.fullmatch(r"[a-z][a-z0-9._-]*", name):
        raise ValueError(f"Invalid module name: {name!r}")
    return name, version


def rewrite_module(text, version, siblings):
    """Rewrite the module to `<version>.envoy` and same-repo release deps to theirs.

    `siblings` maps module names released from the same ref to their versions.
    """
    edits = []
    for name, start, end, attrs in calls(text):
        if name in OVERRIDES:
            line_end = text.find("\n", end)
            line_end = len(text) if line_end < 0 else line_end
            if text[end:line_end].strip() and not text[end:line_end].lstrip().startswith("#"):
                raise ValueError("Override must be a standalone statement")
            edits.append((start, min(line_end + 1, len(text)), ""))
        elif name == "module" or name == "bazel_dep":
            if not {"name", "version"} <= attrs.keys():
                raise ValueError(f"{name} must have literal name and version")
            if name == "module" or attrs["name"][0] in siblings:
                expected = version if name == "module" else siblings[attrs["name"][0]]
                old, start, end = attrs["version"]
                if old != expected:
                    raise ValueError(f"{name} {attrs['name'][0]} version {old} != {expected}")
                literal = text[start:end]
                quote = literal[0]
                if quote not in ("'", '"') or literal.startswith(quote * 3):
                    raise ValueError("Rewritten versions must use simple quoted strings")
                edits.append((start, end, quote + expected + ".envoy" + quote))
    for start, end, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end:]

    # Remove comment-only groups only when they consist entirely of overrides.
    def strip_commented(match):
        block = match[0]
        uncommented = re.sub(r"(?m)^#[ ]?", "", block)
        try:
            found = calls(uncommented)
        except (ValueError, tokenize.TokenError, IndentationError, SyntaxError):
            return block
        if not found or any(name not in OVERRIDES for name, *_ in found):
            return block
        remainder = uncommented
        for _, start, end, _ in reversed(found):
            remainder = remainder[:start] + remainder[end:]
        return "" if not remainder.strip() else block

    return re.sub(r"(?m)(?:^#[^\n]*(?:\n|$))+", strip_commented, text)


def integrity(data):
    return "sha256-" + base64.b64encode(hashlib.sha256(data).digest()).decode()


def render_source(template, repository, ref, version):
    owner, repo = repository.split("/")
    for key, value in {"OWNER": owner, "REPO": repo, "VERSION": version,
                       "TAG": ref.removeprefix("v")}.items():
        template = template.replace("{" + key + "}", value)
    source = json.loads(template)
    if set(source) != {"url", "integrity", "strip_prefix"}:
        raise ValueError("source.template.json must contain only url, integrity, strip_prefix; "
                         "supply patches/overlay through .bcr directories")
    if any(not isinstance(value, str) or re.search(r"\{[^{}]+\}", value)
           for value in source.values()):
        raise ValueError("Unresolved or invalid source template values")
    url = urllib.parse.urlsplit(source["url"])
    if url.scheme != "https" or not url.netloc or url.username or url.password:
        raise ValueError("Source URL must be HTTPS without credentials")
    return {key: source[key] for key in ("url", "integrity", "strip_prefix")}


def archive_member(data, path):
    """Return the bytes of `path` inside a tar (any compression) or zip archive."""
    candidates = (path, "./" + path)
    if zipfile.is_zipfile(io.BytesIO(data)):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for name in candidates:
                if name in archive.namelist():
                    return archive.read(name)
        raise ValueError(f"{path} not found in release archive")
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
            for name in candidates:
                try:
                    member = archive.getmember(name)
                except KeyError:
                    continue
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise ValueError(f"{path} in release archive is not a file")
                return extracted.read()
    except tarfile.ReadError as exc:
        raise ValueError(f"Unsupported release archive: {exc}") from exc
    raise ValueError(f"{path} not found in release archive")


def read_url(url, optional=False, method="GET"):
    headers = {"User-Agent": "envoy-bazel-registry"}
    if urllib.parse.urlsplit(url).hostname == "api.github.com" and os.environ.get("GH_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["GH_TOKEN"]
    try:
        with urllib.request.urlopen(
                urllib.request.Request(url, headers=headers, method=method), timeout=120) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        if optional and exc.code == 404:
            return None
        raise ValueError(f"{method} {url}: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"{method} {url}: {exc.reason}") from exc


class Upstream:
    def __init__(self, repository, ref):
        self.repository = repository
        self.ref = ref

    def raw(self, path, optional=False):
        return read_url(
            f"https://raw.githubusercontent.com/{self.repository}/"
            f"{urllib.parse.quote(self.ref, safe='')}/{path}", optional=optional)

    def tree(self, path):
        url = (f"https://api.github.com/repos/{self.repository}/contents/{path}"
               f"?ref={urllib.parse.quote(self.ref, safe='')}")
        listing = read_url(url, optional=True)
        if listing is None:
            return {}
        entries = json.loads(listing)
        if not isinstance(entries, list):
            raise ValueError(f"{path} must be a directory")
        files = {}
        for entry in sorted(entries, key=lambda e: e["name"]):
            name = safe_path(entry["name"])
            if "/" in name:
                raise ValueError(f"Invalid directory entry: {name}")
            if entry["type"] == "dir":
                files.update({name + "/" + key: value
                              for key, value in self.tree(path + "/" + name).items()})
            elif entry["type"] == "file":
                files[name] = self.raw(path + "/" + name)
            else:
                raise ValueError(f"Unsupported file type in {path}: {name}")
        return files


def registered(root, name, version):
    path = root / name / "metadata.json"
    return ((root / name / version).exists()
            or (path.exists() and version in json.loads(path.read_bytes())["versions"]))


def update_metadata(root, name, version, upstream, bcr):
    path = root / name / "metadata.json"
    if path.exists():
        metadata = json.loads(path.read_bytes())
    else:
        metadata = json.loads(upstream.raw(bcr + "/metadata.template.json"))
        metadata["versions"] = []
        metadata["yanked_versions"] = {}
    if version in metadata["versions"] or (root / name / version).exists():
        raise ValueError(f"{name}@{version} already exists")
    metadata["versions"].append(version)
    return metadata


def check_regression(root, name, source):
    metadata_path = root / name / "metadata.json"
    if not metadata_path.exists():
        return
    versions = json.loads(metadata_path.read_bytes())["versions"]
    if not versions:
        return
    latest = root / name / safe_path(versions[-1]) / "source.json"
    previous = json.loads(latest.read_bytes())
    missing = [f"{kind}/{key}" for kind in ("patches", "overlay")
               for key in previous.get(kind, {}) if key not in source.get(kind, {})]
    if missing:
        raise ValueError(f"{name}: {', '.join(missing)} must be upstreamed to .bcr/ "
                         "in the source repo before registration")


def check_dependencies(root, entries):
    deps = set()
    for entry in entries.values():
        for call, _, _, attrs in calls(entry["MODULE.bazel"].decode()):
            if call == "bazel_dep":
                if not {"name", "version"} <= attrs.keys():
                    raise ValueError("bazel_dep must have literal name and version")
                if attrs.get("dev_dependency"):
                    # Consumers never resolve a dependency's dev dependencies.
                    continue
                deps.add((attrs["name"][0], attrs["version"][0]))
    failures = []
    for name, version in sorted(deps):
        if not re.fullmatch(r"[a-z][a-z0-9._-]*", name):
            raise ValueError(f"Invalid dependency name: {name!r}")
        safe_path(version)
        if version.endswith(".envoy") or ".envoy." in version:
            if ((name, version) not in entries
                    and not (root / name / version / "MODULE.bazel").is_file()):
                failures.append(f"{name}@{version}: missing from this registry")
        else:
            try:
                read_url(f"https://bcr.bazel.build/modules/{name}/{version}/MODULE.bazel",
                         method="HEAD")
            except ValueError as exc:
                failures.append(f"{name}@{version}: BCR verification failed ({exc})")
    if failures:
        raise ValueError("Dependency verification failed:\n" + "\n".join(failures))
    return len(deps)


def register(repository, ref, modules_root, dry_run=False, sha=""):
    """Register each module root whose version at `ref` is a new release.

    Roots at a non-release version (eg `-dev`) or already registered are skipped,
    so one ref can carry modules released independently of each other.
    """
    validate_inputs(repository, ref, sha)
    root = pathlib.Path(modules_root) / "modules"
    if sha:
        commit = json.loads(read_url(
            f"https://api.github.com/repos/{repository}/commits/"
            + urllib.parse.quote(ref, safe="")))
        if commit["sha"].lower() != sha.lower():
            raise ValueError("ref does not resolve to --sha")
    upstream = Upstream(repository, sha or ref)
    # Templates for this registry, when they differ from the repo's BCR ones.
    templates = TEMPLATES
    config = upstream.raw(templates + "/config.yml", optional=True)
    if config is None:
        templates = ".bcr"
        config = upstream.raw(templates + "/config.yml", optional=True)
    roots = module_roots(config.decode() if config is not None else "")
    found = []
    for module_root in roots:
        prefix = "" if module_root == "." else module_root + "/"
        text = upstream.raw(prefix + "MODULE.bazel").decode()
        name, module_version = identity(text)
        found.append((module_root, name, module_version, text))
    if len({name for _, name, _, _ in found}) != len(found):
        raise ValueError("Duplicate module names")
    # Every module released from this ref, registered now or before, so deps
    # between them become `.envoy` either way.
    siblings = {name: v for _, name, v, _ in found if RELEASE.fullmatch(v)}
    candidates, skipped = [], []
    for module_root, name, module_version, text in found:
        if not RELEASE.fullmatch(module_version):
            skipped.append(f"{name}@{module_version}: not a release version")
        elif registered(root, name, module_version + ".envoy"):
            skipped.append(f"{name}@{module_version}.envoy: already registered")
        else:
            candidates.append((module_root, name, module_version, text))
    entries, metadata, archives = {}, {}, {}
    for module_root, name, module_version, text in candidates:
        registry_version = module_version + ".envoy"
        bcr = templates + ("/" + module_root if module_root != "." else "")
        source = render_source(upstream.raw(bcr + "/source.template.json").decode(),
                               repository, ref, module_version)
        if source["url"] not in archives:
            archives[source["url"]] = read_url(source["url"])
        archive = archives[source["url"]]
        source["integrity"] = integrity(archive)
        # The registry's MODULE.bazel must be the one Bazel downloads.
        if archive_member(archive, source["strip_prefix"] + "/MODULE.bazel") != text.encode():
            raise ValueError(f"{name}: MODULE.bazel in {source['url']} differs from {ref}")
        files = {"MODULE.bazel": rewrite_module(text, module_version, siblings).encode()}
        presubmit = upstream.raw(bcr + "/presubmit.yml", optional=True)
        if presubmit is not None:
            files["presubmit.yml"] = presubmit
        for kind in ("patches", "overlay"):
            extra = upstream.tree(bcr + "/" + kind)
            if extra:
                if kind == "patches":
                    source["patch_strip"] = 1
                source[kind] = {path: integrity(data) for path, data in sorted(extra.items())}
                files.update({kind + "/" + path: data for path, data in extra.items()})
        check_regression(root, name, source)
        files["source.json"] = (json.dumps(source, indent=4) + "\n").encode()
        metadata[name] = update_metadata(root, name, registry_version, upstream, bcr)
        entries[name, registry_version] = files
    verified = check_dependencies(root, entries) if entries else 0
    if not dry_run:
        for (name, entry_version), files in entries.items():
            for path, data in files.items():
                target = root / name / entry_version / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            (root / name / "metadata.json").write_text(json.dumps(metadata[name], indent=4) + "\n")
    lines = [("Dry-run; would register:" if dry_run else "Registered:") if entries
             else "Nothing to register."]
    lines += [f"- {name}@{entry_version} ({json.loads(files['source.json'])['integrity']})"
              for (name, entry_version), files in entries.items()]
    if skipped:
        lines += ["Skipped:", *[f"- {reason}" for reason in skipped]]
    if entries:
        lines.append(f"Dependencies verified: {verified}")
    summary = "\n".join(lines)
    print(summary)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            output.write(f"registered={'true' if entries else 'false'}\n")
            if entries:
                (first, first_version), *rest = entries
                label = first + ("+" if rest else "")
                output.write(f"modules={','.join(f'{n}@{v}' for n, v in entries)}\n")
                output.write(f"title=registry/{label}: Add {first_version}\n")
                output.write(f"branch=register/{repository.split('/')[1]}/{first}-{first_version}\n")
            output.write(f"summary<<REGISTER_SUMMARY\n{summary}\nREGISTER_SUMMARY\n")
    return entries


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--sha", default="")
    parser.add_argument("--modules-root", default=".", help="Registry root containing modules/")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        register(args.repository, args.ref, args.modules_root, args.dry_run, args.sha)
    except (ValueError, KeyError, OSError, tokenize.TokenError, SyntaxError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
