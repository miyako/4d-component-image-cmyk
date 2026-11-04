#!/usr/bin/env python3
"""Publish verified signed experimental assets, never an unsigned/stable fallback."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import uuid

from build_provenance import commit, sha256, source_digest, verify
from ci_config import ROOT, TARGETS, load_config
from release_ref import validate_tag, verify as verify_tag


NOTES = """Experimental CMYK release candidate: macOS Developer ID signed and Apple notarized.

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

Native CLI execution passed on macOS ARM64, Intel and Windows x64. The official
pinned tool4d 20.7 source/compiled-package harness and actual compiler passed on
macOS ARM64, including a fresh check of the signed ZIP's unpacked component.
These tool4d results do NOT establish external-component installation/loading in
the normal 4D application. Earlier rc1 wording overstated that scope.
Normal 4D application loading, Intel/Windows 4D and published dependency-manager
installation remain not_run. Evidence intentionally retains result:partial and
release_ready:false; this is not full cross-platform/application acceptance.

The macOS CLI and native library are Developer ID signed with secure timestamps.
CMYK.dmg is separately signed, Apple notarization Accepted, stapled and assessed.
Every macOS payload code object separately passed Apple's online notarization
check and explicit `notarized` requirement. A bare-CLI spctl non-app diagnostic
is retained as a failed applicability probe, not claimed as app acceptance.
CMYK.zip contains the identical signed payload; ZIP itself cannot be stapled.
Windows is unsigned (no Authenticode credentials); no Windows signing claim.
Replace the predecessor component/dependency rather than loading both packages.
The host API is now cs.CMYK.Converter; the former ImageCMYK namespace has no alias.
Download CMYK.zip for the dependency manager, or CMYK.dmg for the notarized macOS
container. CMYK.signing.json binds final delivered hashes and signing/notary gates.
CMYK.acceptance.json and CMYK.native-evidence.json deliberately describe the
pre-sign build/compilation; signing changes native bytes without rewriting those
original compiler markers or manifests. Unsigned rc1 is preserved separately.
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
              "binary_phase": "pre_signing", "targets": targets}
    output = root / "dist" / (config["repository"] + ".native-evidence.json")
    if any(path.is_symlink() for path in [output, *output.parents]):
        raise ValueError("native evidence output must not traverse symlinks")
    output.write_text(json.dumps(result, indent=2) + "\n")
    return output


def validate_evidence(root, expected_sha):
    root = Path(root).resolve()
    config = load_config(root, initialized=True)
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
            or not re.fullmatch(r"[0-9a-f]{64}", artifact.get("sha256", ""))
            or type(artifact["bytes"]) is not int or artifact["bytes"] <= 0):
        raise ValueError("invalid pre-sign acceptance artifact")
    steps = report["steps"]
    for name in ("unit", "icons", "dependencies", "assembly", "source_host",
                 "compile", "package", "compiled_host"):
        if steps[name]["status"] != "passed":
            raise ValueError("mandatory acceptance gate did not pass: " + name)
    for name, compiled in (("source_host", False), ("compiled_host", True)):
        details = steps[name]["details"]
        if (details.get("success") is not True or details.get("finished") is not True
                or details.get("compiled") is not compiled
                or details.get("execution_scope") != "tool4d_harness"
                or details.get("normal_app_loading") != "not_run"):
            raise ValueError("tool4d harness mode evidence is required: " + name)
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
    return assets, report


def presign_assets(root, expected_sha):
    assets, report = validate_evidence(root, expected_sha)
    artifact = report["artifact"]
    if artifact["sha256"] != sha256(assets[0]) or artifact["bytes"] != assets[0].stat().st_size:
        raise ValueError("pre-sign release archive does not match accepted bytes")
    return assets


def publication_assets(root, tag, expected_sha, *, tag_required=True):
    require_prerelease(tag)
    root = Path(root).resolve()
    config = load_config(root, initialized=True, tag=tag)
    if tag_required:
        verify_tag(root, tag, expected_sha)
    assets, acceptance = validate_evidence(root, expected_sha)
    base = root / "dist" / config["repository"]
    dmg = base.with_suffix(".dmg")
    evidence = base.with_suffix(".signing.json")
    for path in (dmg, evidence):
        if not path.is_file() or any(item.is_symlink() for item in [path, *path.parents]):
            raise ValueError("missing regular signed publication asset: " + str(path))
    signed = json.loads(evidence.read_text())
    if (signed.get("schema") != 1 or signed.get("commit") != expected_sha
            or signed.get("version") != config["version"] or signed.get("finished") is not True
            or signed.get("result") != "partial" or signed.get("release_ready") is not False):
        raise ValueError("fresh completed signed experimental evidence is required")
    presign = signed["presign"]
    if (presign.get("artifact") != acceptance["artifact"]
            or presign.get("acceptance_sha256") != sha256(assets[1])
            or presign.get("native_evidence_sha256") != sha256(assets[2])):
        raise ValueError("signed derivative must bind unmodified pre-sign evidence")
    for name, path in (("zip", assets[0]), ("dmg", dmg)):
        record = signed["artifacts"][name]
        if (record.get("path") != path.name or record.get("sha256") != sha256(path)
                or type(record.get("bytes")) is not int or record["bytes"] <= 0
                or record["bytes"] != path.stat().st_size):
            raise ValueError("final signed asset does not match verified bytes: " + name)
    for name in ("payload_signatures", "signed_compiled_harness", "dmg_signature",
                 "notarization", "dmg_stapling", "dmg_assessment", "payload_assessment",
                 "mounted_payload", "cli_smoke"):
        if signed["steps"][name]["status"] != "passed":
            raise ValueError("mandatory signed-distribution gate did not pass: " + name)
    if signed["steps"]["notarization"]["details"].get("status") != "Accepted":
        raise ValueError("Apple notarization must explicitly be Accepted")
    submission = signed["steps"]["notarization"]["details"].get("id", "")
    if not isinstance(submission, str) or str(uuid.UUID(submission)) != submission:
        raise ValueError("Apple notarization submission ID is required")
    harness = signed["steps"]["signed_compiled_harness"]["details"]
    if (harness.get("success") is not True or harness.get("finished") is not True
            or harness.get("compiled") is not True
            or harness.get("execution_scope") != "tool4d_harness"
            or harness.get("normal_app_loading") != "not_run"):
        raise ValueError("exact signed ZIP tool4d harness evidence is required")
    if signed["steps"]["normal_4d_application"]["status"] != "not_run":
        raise ValueError("this workflow cannot claim normal 4D application loading")
    assessment = signed["steps"]["payload_assessment"]["details"]
    expected_objects = {"Libraries/lib4d-arm64.dylib", "Resources/bin/macOS/image-cmyk"}
    objects = assessment["objects"]
    if (assessment.get("method") != "strict_online_notarization" or len(objects) != 2
            or {item["path"] for item in objects} != expected_objects
            or any(item.get("status") != "passed"
                   or item.get("command", [])[:-1] != [
                       "/usr/bin/codesign", "--verify", "--strict", "--all-architectures",
                       "--check-notarization", "-R=notarized"] for item in objects)):
        raise ValueError("explicit online notarized requirement must pass for both payload code objects")
    probe = assessment["cli_spctl"]
    if probe.get("status") == "passed":
        if probe.get("accepted") is not True or probe.get("source") != "Notarized Developer ID":
            raise ValueError("invalid accepted CLI Gatekeeper probe")
    elif not (probe.get("status") == "failed" and probe.get("applicable") is False
              and probe.get("classification") == "valid_non_app" and probe.get("attempted") is True
              and probe.get("exit_code") == 3
              and probe.get("diagnostic") == "the code is valid but does not seem to be an app"):
        raise ValueError("unclassified CLI Gatekeeper rejection remains a blocker")
    validate_payload_evidence(signed)
    return [*assets, dmg, evidence]


def validate_payload_evidence(signed):
    certificate = signed.get("certificate_sha1", "")
    if not re.fullmatch(r"[0-9A-Fa-f]{40}", certificate):
        raise ValueError("verified Developer ID certificate fingerprint is required")
    payload = signed["payload"]
    before, after = payload["before"], payload["after"]
    expected = {"Libraries/lib4d-arm64.dylib": {"arm64"},
                "Resources/bin/macOS/image-cmyk": {"arm64", "x86_64"}}
    if not before or set(before) != set(after) or not set(expected).issubset(after):
        raise ValueError("complete identical before/after payload paths are required")
    for path in before:
        for record in (before[path], after[path]):
            if (not re.fullmatch(r"[0-9a-f]{64}", record.get("sha256", ""))
                    or type(record.get("bytes")) is not int or record["bytes"] <= 0
                    or type(record.get("mode")) is not int or not 0 <= record["mode"] <= 0o777):
                raise ValueError("invalid signed payload inventory")
        if (before[path]["mode"] != after[path]["mode"]
                or path not in expected and before[path] != after[path]):
            raise ValueError("signing changed non-code payload or file permissions")
    objects = payload["code_objects"]
    if len(objects) != len(expected) or {item["path"] for item in objects} != set(expected):
        raise ValueError("exact macOS code object inventory is required")
    for item in objects:
        path = item["path"]
        if (set(item["architectures"]) != expected[path]
                or item.get("before_sha256") != before[path]["sha256"]
                or item.get("sha256") != after[path]["sha256"]
                or item.get("certificate_sha1") != certificate
                or any(item.get(flag) is not True
                       for flag in ("team_match", "timestamp", "hardened_runtime"))):
            raise ValueError("invalid Developer ID code object evidence")


def verify_uploaded_assets(root, tag, assets):
    with tempfile.TemporaryDirectory(prefix="cmyk-release-download-") as directory:
        for asset in assets:
            subprocess.run(["gh", "release", "download", tag, "--pattern", asset.name,
                            "--dir", directory], cwd=root, check=True)
            downloaded = Path(directory) / asset.name
            if downloaded.stat().st_size != asset.stat().st_size or sha256(downloaded) != sha256(asset):
                raise ValueError("uploaded draft asset differs from validated local bytes: " + asset.name)


def release_metadata(root, tag):
    # The by-tag endpoint excludes drafts, even when the caller can list them.
    result = subprocess.run(["gh", "api", "repos/{owner}/{repo}/releases?per_page=100",
                             "--paginate", "--slurp"],
                            cwd=root, check=True, capture_output=True, text=True)
    matching = [release for page in json.loads(result.stdout) for release in page
                if release.get("tag_name") == tag]
    if len(matching) != 1:
        raise ValueError("exactly one owned draft or published release must match the tag")
    return matching[0]


def publish(root, tag, expected_sha):
    assets = publication_assets(root, tag, expected_sha)
    subprocess.run(["gh", "release", "create", tag, *(str(path) for path in assets),
                    "--verify-tag", "--draft", "--prerelease", "--latest=false",
                    "--title", "CMYK " + tag + " (signed macOS experimental)",
                    "--notes", NOTES], cwd=root, check=True)
    verify_uploaded_assets(root, tag, assets)
    subprocess.run(["git", "fetch", "--force", "--no-tags", "origin",
                    "+refs/tags/" + tag + ":refs/tags/" + tag], cwd=root, check=True)
    verify_tag(root, tag, expected_sha)
    draft = release_metadata(root, tag)
    if draft.get("draft") is not True or draft.get("prerelease") is not True:
        raise ValueError("release must remain a draft prerelease until uploaded-byte verification")
    subprocess.run(["gh", "release", "edit", tag, "--draft=false",
                    "--prerelease", "--latest=false"], cwd=root, check=True)
    final = release_metadata(root, tag)
    if final.get("draft") is not False or final.get("prerelease") is not True:
        raise ValueError("published release is not the authorized public prerelease")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write-native-evidence", action="store_true")
    mode.add_argument("--validate-signed", action="store_true",
                      help="validate final signed bytes without creating a tag or publishing")
    parser.add_argument("--tag")
    parser.add_argument("--expected-sha")
    args = parser.parse_args()
    try:
        if args.write_native_evidence:
            print(write_native_evidence(args.root))
        else:
            if not args.tag or not args.expected_sha:
                raise ValueError("publication requires tag and expected SHA")
            if args.validate_signed:
                publication_assets(args.root, args.tag, args.expected_sha, tag_required=False)
                print("Final signed distribution evidence validated; no tag or publication created.")
            else:
                publish(args.root, args.tag, args.expected_sha)
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as error:
        parser.exit(1, f"prerelease publication failed: {error}\n")


if __name__ == "__main__":
    main()
