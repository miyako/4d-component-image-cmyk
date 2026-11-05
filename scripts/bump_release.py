#!/usr/bin/env python3
"""Compute the next release version from existing tags and write it to the repository.

next  prints the version Publish will release: the latest stable SemVer tag
      (legacy "v"-prefixed tags accepted, prereleases ignored) bumped by
      --mode patch|minor|major. Without any stable tag, package.json is the base.
apply synchronizes package.json, template.json, the host contract and the class
      documentation version example. Applying the current version is a no-op.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from ci_config import ROOT, load_config


MODES = ("patch", "minor", "major")
HOST_CONTRACT = "tests/host/contract.json"
CLASS_DOC = "component/CMYK/Documentation/Classes/Converter.md"
VERSION_FILES = ("package.json", "template.json", HOST_CONTRACT, CLASS_DOC)
STABLE_TAG = re.compile(r"v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z", re.ASCII)
CORE = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:[-+].*)?\Z", re.ASCII)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def json_object(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "duplicate JSON object key")
            result[key] = value
        return result
    result = json.loads(data.decode("utf-8"), object_pairs_hook=unique)
    require(isinstance(result, dict), "JSON object required")
    return result



def patch_json(data, changes):
    """Replace only selected JSON value spans, retaining unrelated bytes/format."""
    decoder = json.JSONDecoder()
    source = data.decode("utf-8")
    spans = []
    seen = set()

    def whitespace(index):
        while index < len(source) and source[index] in " \t\r\n":
            index += 1
        return index

    def walk(index, path):
        index = whitespace(index)
        if path in changes:
            _, end = decoder.raw_decode(source, index)
            spans.append((index, end, json.dumps(changes[path], ensure_ascii=False, separators=(",", ":"))))
            seen.add(path)
            return end
        if source[index] == "{":
            index = whitespace(index + 1)
            if source[index] == "}":
                return index + 1
            while True:
                key, index = decoder.raw_decode(source, index)
                index = whitespace(index)
                require(source[index] == ":", "invalid JSON separator")
                index = whitespace(walk(index + 1, path + (key,)))
                if source[index] == "}":
                    return index + 1
                require(source[index] == ",", "invalid JSON object")
                index = whitespace(index + 1)
        if source[index] == "[":
            index, number = whitespace(index + 1), 0
            if source[index] == "]":
                return index + 1
            while True:
                index = whitespace(walk(index, path + (number,)))
                number += 1
                if source[index] == "]":
                    return index + 1
                require(source[index] == ",", "invalid JSON array")
                index = whitespace(index + 1)
        return decoder.raw_decode(source, index)[1]

    json_object(data)
    walk(0, ())
    require(seen == set(changes), "required version JSON field missing")
    for start, end, value in sorted(spans, reverse=True):
        source = source[:start] + value + source[end:]
    return source.encode("utf-8")


def version_changes(files, previous, version):
    """Recompute the complete, bounded version-only patch from original blobs."""
    result = {}
    for name in ("package.json", "template.json"):
        original = json_object(files[name])
        require(original.get("version") == previous, "base package/template versions are not synchronized")
        result[name] = patch_json(files[name], {("version",): version})
    package, template = (json_object(files[name]) for name in ("package.json", "template.json"))
    require(package.get("name") == "CMYK" and template.get("repository") == "CMYK"
            and template.get("namespace") == "CMYK" and template.get("cli") == "image-cmyk"
            and template.get("initialized") is True, "base candidate identity is not CMYK")
    contract = json_object(files[HOST_CONTRACT])
    old_stdout = json.dumps({"protocol": 1, "version": previous}, separators=(",", ":")) + "\n"
    new_stdout = json.dumps({"protocol": 1, "version": version}, separators=(",", ":")) + "\n"
    changes, found = {}, {"stdout": 0, "value": 0, "renamed": 0}
    for section in ("feature", "execution"):
        for index, assertion in enumerate(contract.get(section, {}).get("assertions", [])):
            pointer = assertion.get("path")
            prefix = (section, "assertions", index, "equals")
            if pointer == "/version/stdout":
                require(assertion.get("equals") == old_stdout, "stale host version stdout")
                changes[prefix] = new_stdout
                found["stdout"] += 1
            elif pointer == "/version/value":
                require(assertion.get("equals") == {"protocol": 1, "version": previous}, "stale host decoded version")
                changes[prefix + ("version",)] = version
                found["value"] += 1
            elif pointer == "/renamedExecutable":
                value = assertion.get("equals")
                require(isinstance(value, dict) and value.get("stdout") == old_stdout,
                        "stale renamed-executable version stdout")
                changes[prefix + ("stdout",)] = new_stdout
                found["renamed"] += 1
    require(found == {"stdout": 1, "value": 1, "renamed": 1}, "exact host version assertions are required")
    result[HOST_CONTRACT] = patch_json(files[HOST_CONTRACT], changes)
    if CLASS_DOC in files:
        source = files[CLASS_DOC].decode("utf-8")
        pattern = re.compile(r'(?m)^(\| `version\(\) : Object` \| `\{protocol:1,version:")([^"]+)("\}` \|)\r?$')
        matches = list(pattern.finditer(source))
        require(len(matches) <= 1, "ambiguous class version example")
        if matches:
            match = matches[0]
            require(match[2] == previous, "stale class version example")
            result[CLASS_DOC] = (source[:match.start(2)] + version + source[match.end(2):]).encode("utf-8")
    return result


def latest_tag_version(tags):
    """Highest stable X.Y.Z (or legacy vX.Y.Z) tag as a tuple, or None."""
    versions = [tuple(int(part) for part in match.groups())
                for match in (STABLE_TAG.fullmatch(tag.strip()) for tag in tags) if match]
    return max(versions) if versions else None


def bump(base, mode):
    require(mode in MODES, "mode must be patch, minor or major")
    major, minor, patch = base
    if mode == "major":
        return f"{major + 1}.0.0"
    if mode == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def next_version(root, mode, tags):
    base = latest_tag_version(tags)
    if base is None:
        match = CORE.fullmatch(load_config(root)["version"])
        require(match is not None, "package.json version must be SemVer")
        base = tuple(int(part) for part in match.groups())
    return bump(base, mode)


def git_tags(root):
    result = subprocess.run(["git", "tag", "--list"], cwd=root, check=True,
                            capture_output=True, text=True)
    return result.stdout.splitlines()


def apply(root, version):
    root = Path(root)
    require(STABLE_TAG.fullmatch(version) and not version.startswith("v"), "version must be bare X.Y.Z")
    previous = load_config(root)["version"]
    if previous == version:
        return []
    files = {name: (root / name).read_bytes() for name in VERSION_FILES if (root / name).is_file()}
    require(all(name in files for name in VERSION_FILES[:3]), "required version files missing")
    changed = version_changes(files, previous, version)
    for name, data in changed.items():
        (root / name).write_bytes(data)
    load_config(root)
    return sorted(changed)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("next", "apply"))
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--mode", choices=MODES, default="patch")
    parser.add_argument("--version", help="bare X.Y.Z version for apply")
    args = parser.parse_args()
    try:
        if args.command == "next":
            version = next_version(args.root, args.mode, git_tags(args.root))
            print(version)
            if output := os.environ.get("GITHUB_OUTPUT"):
                with open(output, "a", encoding="utf-8") as stream:
                    stream.write(f"version={version}\n")
        else:
            require(args.version, "apply requires --version")
            for name in apply(args.root, args.version):
                print(name)
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"bump_release: {error}\n")


if __name__ == "__main__":
    main()
