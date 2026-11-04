#!/usr/bin/env python3
"""Publish only an explicitly tagged, accepted unsigned experimental prerelease."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from build_provenance import commit, sha256, source_digest, verify
from ci_config import ROOT, TARGETS, load_config
from release_ref import validate_tag, verify as verify_tag


NOTES = """Unsigned experimental CMYK release candidate.

```4d
var $converter : cs.CMYK.Converter
$converter:=cs.CMYK.Converter.new()
```

Convert original files with `convertFile(source; destination; options)`, supplying
a licensed CMYK ICC as the required `options.destinationProfile` File.
Inspect with `inspectFile(file)`. No proprietary press profile is bundled.

Input: 8-bit RGB/CMYK JPEG, non-interlaced/non-animated RGB PNG, or bounded
single-page classic stripped contiguous RGB/CMYK TIFF. Output: CMYK TIFF/JPEG
with exact destination ICC bytes. Alpha/transparency, non-top-left orientation,
unsupported depths/layouts and existing destinations are rejected. Untagged RGB
requires an explicit source ICC or explicit sRGB assumption; CMYK requires a profile.
No resizing, PDF, spot-color, separations, soft-proof or color-lossless promise.

Native CLI execution passed on macOS ARM64, Intel and Windows x64. Actual source
and exact unpacked compiled-component host acceptance passed on macOS ARM64 with
official pinned tool4d 20.7. Intel/Windows 4D and published dependency-manager
installation remain not_run. The attached acceptance report intentionally retains
result:partial and release_ready:false; this is not full cross-platform acceptance.

UNSIGNED / NOT NOTARIZED. Signing and notarization are not_run.
Replace the predecessor component/dependency rather than loading both packages.
The host API is now cs.CMYK.Converter; the former ImageCMYK namespace has no alias.
Download CMYK.zip; adjacent JSON files bind its hash and native/build/host evidence.
"""


def require_prerelease(tag):
    validate_tag(tag)
    if "-" not in tag[1:].split("+", 1)[0]:
        raise ValueError("this experimental workflow only publishes prerelease SemVer tags")


def write_native_evidence(root):
    root = Path(root)
    config = load_config(root, initialized=True)
    targets = {}
    for target in TARGETS:
        filename = config["cli"] + (".exe" if target == "windows-x64" else "")
        manifest = verify(root, target, root / "build" / target / filename)
        if manifest.get("execution") != "passed":
            raise ValueError("native execution evidence is required: " + target)
        targets[target] = manifest
    result = {"schema": 1, "commit": commit(root), "source_sha256": source_digest(root),
              "run_url": os.environ.get("GITHUB_SERVER_URL", "https://github.com") + "/"
                         + os.environ.get("GITHUB_REPOSITORY", "") + "/actions/runs/"
                         + os.environ.get("GITHUB_RUN_ID", ""),
              "targets": targets}
    output = root / "dist" / (config["repository"] + ".native-evidence.json")
    if any(path.is_symlink() for path in [output, *output.parents]):
        raise ValueError("native evidence output must not traverse symlinks")
    output.write_text(json.dumps(result, indent=2) + "\n")
    return output


def publication_assets(root, tag, expected_sha):
    require_prerelease(tag)
    root = Path(root)
    config = load_config(root, initialized=True, tag=tag)
    verify_tag(root, tag, expected_sha)
    if commit(root) != expected_sha:
        raise ValueError("publication checkout must match the resolved tag")
    base = root / "dist" / config["repository"]
    assets = [base.with_suffix(".zip"), base.with_suffix(".acceptance.json"),
              base.with_suffix(".native-evidence.json")]
    for path in assets:
        if not path.is_file() or any(item.is_symlink() for item in [path, *path.parents]):
            raise ValueError("missing regular publication asset: " + str(path))
    report = json.loads(assets[1].read_text())
    if (report.get("configuration") != config or report.get("commit") != expected_sha
            or report.get("finished") is not True
            or report.get("result") != "partial" or report.get("release_ready") is not False):
        raise ValueError("fresh completed experimental acceptance is required")
    artifact = report["artifact"]
    if (Path(artifact["path"]).name != assets[0].name
            or artifact["sha256"] != sha256(assets[0])
            or type(artifact["bytes"]) is not int or artifact["bytes"] <= 0
            or artifact["bytes"] != assets[0].stat().st_size):
        raise ValueError("release archive does not match accepted bytes")
    steps = report["steps"]
    for name in ("unit", "icons", "dependencies", "assembly", "source_host",
                 "compile", "package", "compiled_host"):
        if steps[name]["status"] != "passed":
            raise ValueError("mandatory acceptance gate did not pass: " + name)
    for name, compiled in (("source_host", False), ("compiled_host", True)):
        details = steps[name]["details"]
        if (details.get("success") is not True or details.get("finished") is not True
                or details.get("compiled") is not compiled):
            raise ValueError("real host mode evidence is required: " + name)
    compiler = steps["compile"]["details"]
    if (compiler.get("success") is not True or compiler.get("errors") != []
            or set(compiler.get("targets", [])) != {"x86_64_generic", "arm64_macOS_lib"}
            or any(not re.fullmatch(r"[0-9a-f]{64}", compiler.get(field, ""))
                   for field in ("input_sha256", "output_sha256"))):
        raise ValueError("successful digest-bound compilation evidence is required")
    native = json.loads(assets[2].read_text())
    digest = source_digest(root)
    if native.get("commit") != expected_sha or native.get("source_sha256") != digest:
        raise ValueError("native evidence does not match tagged source")
    if set(native["targets"]) != set(TARGETS):
        raise ValueError("all three native targets are required")
    for target, manifest in native["targets"].items():
        if (manifest.get("target") != target or manifest.get("commit") != expected_sha
                or manifest.get("source_sha256") != digest or manifest.get("execution") != "passed"):
            raise ValueError("invalid native execution provenance: " + target)
    return assets


def publish(root, tag, expected_sha):
    assets = publication_assets(root, tag, expected_sha)
    subprocess.run(["gh", "release", "create", tag, *(str(path) for path in assets),
                    "--verify-tag", "--prerelease", "--latest=false",
                    "--title", "CMYK " + tag + " (unsigned experimental)",
                    "--notes", NOTES], cwd=root, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--write-native-evidence", action="store_true")
    parser.add_argument("--tag")
    parser.add_argument("--expected-sha")
    args = parser.parse_args()
    try:
        if args.write_native_evidence:
            print(write_native_evidence(args.root))
        else:
            if not args.tag or not args.expected_sha:
                raise ValueError("publication requires tag and expected SHA")
            publish(args.root, args.tag, args.expected_sha)
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as error:
        parser.exit(1, f"prerelease publication failed: {error}\n")


if __name__ == "__main__":
    main()
