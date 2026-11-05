#!/usr/bin/env python3
"""Fail-closed CI-only Developer ID signing of an accepted CMYK derivative.

The original compiler outputs and unsigned evidence are never rewritten. Only
the two external Mach-O code objects are signed; the nested .4DZ is opaque.
This script neither publishes assets nor establishes normal 4D app loading.
Untagged main dispatch preflight is bound to GITHUB_SHA and creates no tag.
"""
import argparse
import base64
import binascii
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import plistlib
import re
import shlex
import shutil
import stat
import subprocess
import sys
import uuid
import zipfile

from build_provenance import commit, sha256, source_digest, verify
from ci_config import ROOT, TARGETS, load_config, tag_version
from package_component import validate_compile, write_zip
from publish_prerelease import presign_assets


CODE = {"Resources/bin/macOS/image-cmyk": {"arm64", "x86_64"},
        "Libraries/lib4d-arm64.dylib": {"arm64"}}
MAGIC = {b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf",
         b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",
         b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca"}
SECRETS = ("APPLE_DEVELOPER_ID_CERTIFICATE", "APPLE_DEVELOPER_ID_CERTIFICATE_PASSWORD",
           "KEYCHAIN_PASSWORD", "NOTARYTOOL_APPLE_ID", "NOTARYTOOL_PASSWORD",
           "NOTARYTOOL_TEAM_ID")


class SigningError(RuntimeError):
    """Only fixed, non-sensitive diagnostic labels may cross this boundary."""


def require(condition, label):
    if not condition:
        raise SigningError(label)


def capture(label, arguments, *, cwd=None, timeout=120, data=None, env=None):
    """Never expose argv, captured output, or subprocess exception text."""
    try:
        clean_env = {key: value for key, value in (os.environ if env is None else env).items()
                     if key not in SECRETS}
        process = subprocess.run(arguments, cwd=cwd, input=data, capture_output=True,
                                 timeout=timeout, check=False, env=clean_env)
    except subprocess.TimeoutExpired:
        raise SigningError(label + ": timeout") from None
    except OSError:
        raise SigningError(label + ": command unavailable") from None
    return process


def run(label, arguments, *, cwd=None, timeout=120, data=None, env=None):
    process = capture(label, arguments, cwd=cwd, timeout=timeout, data=data, env=env)
    if process.returncode:
        diagnostic = (process.stdout + process.stderr).decode("utf-8", errors="replace")
        categories = (
            ("errSecInternalComponent", "keychain or certificate trust failure"),
            ("unable to build chain to self-signed root", "certificate chain unavailable"),
            ("User interaction is not allowed", "noninteractive private-key access denied"),
            ("The specified item could not be found in the keychain", "keychain item unavailable"),
            ("The timestamp service is not available", "secure timestamp service unavailable"),
            ("resource fork, Finder information, or similar detritus", "unsupported filesystem metadata"),
            ("invalid or unsuitable", "unsuitable signing object"),
        )
        hints = [category for token, category in categories if token in diagnostic]
        hint = "; ".join(hints) if hints else "unclassified command failure"
        raise SigningError(label + ": nonzero exit " + str(process.returncode) + " (" + hint + ")")
    return process.stdout, process.stderr


def text(data):
    try:
        return data.decode("utf-8", errors="strict")
    except UnicodeError:
        raise SigningError("command returned invalid UTF-8") from None


def regular(path):
    require(not any(item.is_symlink() for item in (path, *path.parents)),
            "symlink path prohibited")
    require(path.is_file() and stat.S_ISREG(path.stat().st_mode),
            "required regular file missing")


def read_json(path):
    regular(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError):
        raise SigningError("invalid evidence JSON") from None
    require(isinstance(value, dict), "evidence must be an object")
    return value


def state_path(root):
    root = Path(root).absolute()
    path = root / "build/signing-state.json"
    require(not any(item.is_symlink() for item in (path, *path.parents)), "cleanup state symlinks prohibited")
    return path


def create_cleanup_state(root, scratch):
    path = state_path(root)
    require(not path.exists(), "unfinished signing cleanup state already exists; run --cleanup")
    relative = scratch.relative_to(root).as_posix()
    require(re.fullmatch(r"build/signing-[0-9a-f]{32}", relative), "invalid private staging prefix")
    require(not scratch.exists(), "private staging directory already exists")
    state = {"schema": 1, "staging": relative, "keychain": relative + "/signing.keychain-db",
             "p12": relative + "/certificate.p12", "mount": relative + "/mounted",
             "search_list_changed": False, "original_search_list": []}
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(state, indent=2) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def validate_cleanup_state(root, state):
    require(set(state) == {"schema", "staging", "keychain", "p12", "mount",
                           "search_list_changed", "original_search_list"}
            and type(state["schema"]) is int and state["schema"] == 1
            and isinstance(state["staging"], str)
            and re.fullmatch(r"build/signing-[0-9a-f]{32}", state["staging"]),
            "invalid signing cleanup state")
    for field, basename in (("keychain", "signing.keychain-db"), ("p12", "certificate.p12"), ("mount", "mounted")):
        require(state[field] == state["staging"] + "/" + basename, "cleanup path outside known staging prefix")
    require(type(state["search_list_changed"]) is bool and isinstance(state["original_search_list"], list)
            and all(isinstance(item, str) and Path(item).is_absolute()
                    and not any(ord(character) < 32 for character in item)
                    for item in state["original_search_list"])
            and (state["search_list_changed"] or state["original_search_list"] == []),
            "invalid original keychain search-list metadata")
    paths = {field: root.joinpath(*PurePosixPath(state[field]).parts)
             for field in ("staging", "keychain", "p12", "mount")}
    for candidate in paths.values():
        require(not any(item.is_symlink() for item in (candidate, *candidate.parents)),
                "cleanup target symlinks prohibited")
    return paths


def cleanup(root):
    """Idempotent cancellation backup. Inspect only our journal, never credentials."""
    root = Path(root).absolute()
    journal = state_path(root)
    if not journal.exists():
        return {"status": "not_needed"}
    regular(journal)
    require(journal.stat().st_size <= 16384, "cleanup journal exceeds size limit")
    state = read_json(journal)
    paths = validate_cleanup_state(root, state)
    require(not any(os.path.ismount(paths[field]) for field in ("staging", "keychain", "p12")),
            "unexpected mount on cleanup target")
    failures = []
    # Attempt independent cleanup actions even if another action fails. Keep the
    # journal and private directory until every active resource is removed.
    try:
        paths["p12"].unlink(missing_ok=True)
    except OSError:
        failures.append("P12 removal failed")
    if paths["keychain"].exists():
        try:
            regular(paths["keychain"])
            run("delete journaled signing keychain",
                ["/usr/bin/security", "delete-keychain", str(paths["keychain"])])
            require(not paths["keychain"].exists(), "journaled signing keychain still exists")
        except (OSError, SigningError):
            failures.append("keychain removal failed")
    if os.path.ismount(paths["mount"]):
        try:
            run("detach journaled signing mount",
                ["/usr/bin/hdiutil", "detach", str(paths["mount"])], timeout=120)
            require(not os.path.ismount(paths["mount"]), "journaled signing mount remains active")
        except (OSError, SigningError):
            failures.append("mount detach failed")
    if state["search_list_changed"]:
        try:
            run("restore original keychain search list",
                ["/usr/bin/security", "list-keychains", "-d", "user", "-s", *state["original_search_list"]])
        except SigningError:
            failures.append("search-list restoration failed")
    require(not failures, "signing cleanup failed: " + "; ".join(failures))
    staging = paths["staging"]
    if staging.exists():
        require(staging.is_dir(), "cleanup staging target is not a directory")
        # Never recurse into an active mount (including an unexpected nested
        # mount) or symlink. Detach must have succeeded before walking this tree.
        for directory, names, files in os.walk(staging, topdown=True, followlinks=False):
            parent = Path(directory)
            require(not os.path.ismount(parent) and not parent.is_symlink(), "mounted cleanup tree prohibited")
            for name in [*names, *files]:
                child = parent / name
                require(not child.is_symlink() and not os.path.ismount(child), "unsafe nested cleanup target")
                require(child.is_dir() or child.is_file(), "nonregular cleanup target prohibited")
        shutil.rmtree(staging)
    journal.unlink()
    return {"status": "passed"}


def artifact(path):
    regular(path)
    return {"path": path.name, "sha256": sha256(path), "bytes": path.stat().st_size}


def snapshot_keychain_search_list(root):
    out, _ = run("snapshot original keychain search list",
                 ["/usr/bin/security", "list-keychains", "-d", "user"])
    try:
        original = shlex.split(text(out))
    except ValueError:
        raise SigningError("invalid original keychain search-list quoting") from None
    require(original and len(original) == len(set(original))
            and all(Path(path).is_absolute() and not any(ord(char) < 32 for char in path)
                    for path in original), "invalid original keychain search list")
    journal = state_path(root)
    state = read_json(journal)
    validate_cleanup_state(root, state)
    require(state["search_list_changed"] is False, "search list already journaled")
    # Journal before creation: security create-keychain may itself add an entry.
    state.update(search_list_changed=True, original_search_list=original)
    journal.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return original


def preflight(root, tag, expected_sha, env=None, system=None, candidate=None):
    env = os.environ if env is None else env
    require((platform.system() if system is None else system) == "Darwin",
            "production signing requires macOS")
    from bump_release import release_channel, validate_candidate
    try:
        release_channel(tag)
    except ValueError:
        raise SigningError("normalized SemVer release tag required") from None
    require(isinstance(expected_sha, str) and re.fullmatch(r"[0-9a-f]{40}", expected_sha),
            "expected full commit SHA required")
    require(commit(root) == expected_sha, "checkout HEAD differs from expected SHA")
    require(env.get("GITHUB_ACTIONS") == "true", "production GitHub Actions required")
    require(env.get("GITHUB_REPOSITORY") == "miyako/CMYK", "production destination must be miyako/CMYK")
    event = env.get("GITHUB_EVENT_NAME")
    if event == "push":
        require(env.get("GITHUB_REF") == "refs/tags/" + tag
                and env.get("GITHUB_SHA") == expected_sha, "push must identify exact release tag")
    elif event == "workflow_dispatch":
        require(env.get("GITHUB_REF") == "refs/heads/main",
                "manual production dispatch must originate on main")
    else:
        raise SigningError("only tag push or main workflow_dispatch is allowed")
    if candidate is not None:
        require(event == "workflow_dispatch", "version candidates require main manual dispatch")
        metadata = validate_candidate(root, candidate, expected_sha, production=True)
        require(metadata["tag"] == tag, "version candidate differs from signing tag")
        config = load_config(root, initialized=True, tag=tag, repository=env.get("GITHUB_REPOSITORY"))
        require(config["repository"] == "CMYK" and config["cli"] == "image-cmyk",
                "signer is restricted to CMYK")
        return config
    tag_ref = "refs/tags/" + tag
    tag_exists = True
    if event == "workflow_dispatch":
        refs, _ = run("inspect optional preflight tag",
                      ["git", "for-each-ref", "--format=%(refname)", tag_ref], cwd=root)
        names = text(refs).splitlines()
        require(not names or names == [tag_ref], "ambiguous preflight tag reference")
        tag_exists = bool(names)
        if not tag_exists:
            require(env.get("GITHUB_SHA") == expected_sha,
                    "untagged preflight must sign exact dispatched main SHA")
    if tag_exists:
        resolved, _ = run("resolve release tag",
                          ["git", "rev-parse", "--verify", tag_ref + "^{commit}"], cwd=root)
        require(text(resolved).strip() == expected_sha, "release tag differs from expected SHA")
    config = load_config(root, initialized=True, tag=tag, repository=env.get("GITHUB_REPOSITORY"))
    require(config["repository"] == "CMYK" and config["cli"] == "image-cmyk",
            "signer is restricted to CMYK")
    return config


def validate_presign(root, config, expected_sha):
    # This gate validates unsigned inputs only; publication_assets requires the
    # final signed derivative and must never be used at this stage.
    presign_assets(root, expected_sha)
    base = root / "dist"
    archive = base / "CMYK.zip"
    acceptance_path = base / "CMYK.acceptance.json"
    native_path = base / "CMYK.native-evidence.json"
    acceptance = read_json(acceptance_path)
    native = read_json(native_path)
    actual = artifact(archive)
    accepted = acceptance.get("artifact", {})
    require(acceptance.get("schema") == 1 and acceptance.get("configuration") == config
            and acceptance.get("commit") == expected_sha and acceptance.get("finished") is True
            and acceptance.get("result") == "partial" and acceptance.get("release_ready") is False,
            "completed same-revision unsigned acceptance required")
    require(isinstance(accepted.get("path"), str)
            and Path(accepted["path"]).name == actual["path"]
            and accepted.get("sha256") == actual["sha256"]
            and type(accepted.get("bytes")) is int and accepted["bytes"] == actual["bytes"]
            and actual["bytes"] > 0, "unsigned archive differs from accepted bytes")
    steps = acceptance.get("steps", {})
    for name in ("unit", "icons", "dependencies", "assembly", "source_host",
                 "compile", "package", "compiled_host"):
        require(steps.get(name, {}).get("status") == "passed", "unsigned acceptance gate missing: " + name)
    for name, compiled in (("source_host", False), ("compiled_host", True)):
        details = steps[name].get("details", {})
        require(details.get("success") is True and details.get("finished") is True
                and details.get("compiled") is compiled
                and details.get("execution_scope") == "tool4d_harness"
                and details.get("normal_app_loading") == "not_run", "unsigned harness mode or scope invalid")
    compiler = steps["compile"].get("details", {})
    require(compiler.get("success") is True and compiler.get("errors") == []
            and set(compiler.get("targets", [])) == {"x86_64_generic", "arm64_macOS_lib"}
            and all(isinstance(compiler.get(key), str) and re.fullmatch(r"[0-9a-f]{64}", compiler[key])
                    for key in ("input_sha256", "output_sha256")),
            "unsigned compile evidence invalid")
    # Validate the untouched original markers, not signed derivative hashes.
    validate_compile(root, root / "component/CMYK", "CMYK")
    marker = read_json(root / "build/compile-status.json")
    require(all(marker.get(key) == compiler[key] for key in ("input_sha256", "output_sha256")),
            "unsigned compiler evidence differs from original marker")
    digest = source_digest(root)
    require(native.get("schema") == 1 and native.get("commit") == expected_sha
            and native.get("source_sha256") == digest
            and set(native.get("targets", {})) == set(TARGETS), "native source evidence invalid")
    for target in TARGETS:
        manifest = native["targets"][target]
        executable = root / "build" / target / ("image-cmyk.exe" if target == "windows-x64" else "image-cmyk")
        require(manifest == verify(root, target, executable) and manifest.get("execution") == "passed",
                "native manifest or native execution mismatch")
    tool4d_sha256 = acceptance.get("tool4d", {}).get("binary_sha256")
    require(isinstance(tool4d_sha256, str) and re.fullmatch(r"[0-9a-f]{64}", tool4d_sha256),
            "original tool4d binary provenance required")
    return {"artifact": accepted, "acceptance_sha256": sha256(acceptance_path),
            "native_evidence_sha256": sha256(native_path),
            "compile_marker_sha256": sha256(root / "build/compile-status.json"),
            "compiler_input_sha256": compiler["input_sha256"],
            "compiler_output_sha256": compiler["output_sha256"],
            "tool4d_sha256": tool4d_sha256,
            "source_sha256": digest}


def zip_entries(data, prefix=None):
    """Validate path/type/duplicate/mode and explicit parent records before use."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names, entries, directories = set(), [], set()
            for info in archive.infolist():
                path = PurePosixPath(info.filename)
                mode = info.external_attr >> 16
                require(path.parts and not path.is_absolute() and ".." not in path.parts
                        and "\\" not in info.filename and ":" not in info.filename
                        and not any(ord(character) < 32 or ord(character) == 127 for character in info.filename)
                        and path.as_posix() + ("/" if info.is_dir() else "") == info.filename
                        and (prefix is None or path.parts[0] == prefix)
                        and info.filename.casefold() not in names
                        and stat.S_IFMT(mode) == (stat.S_IFDIR if info.is_dir() else stat.S_IFREG)
                        and mode & 0o777 in (0o644, 0o755)
                        and not mode & 0o7000 and not info.flag_bits & 1,
                        "unsafe archive entry")
                names.add(info.filename.casefold())
                if info.is_dir():
                    directories.add(info.filename)
                else:
                    entries.append((info.filename, archive.read(info), mode & 0o777))
            for name, _, _ in entries:
                require(all(parent.as_posix() + "/" in directories for parent in PurePosixPath(name).parents
                            if parent.as_posix() != "."), "ZIP requires explicit directory records")
            require(entries, "empty archive prohibited")
            return entries
    except (zipfile.BadZipFile, RuntimeError):
        raise SigningError("invalid ZIP") from None


def extract(archive, directory):
    regular(archive)
    require(not directory.exists() and not directory.is_symlink(), "extract destination already exists")
    entries = zip_entries(archive.read_bytes(), "CMYK.4dbase")
    nested = [content for name, content, _ in entries if name == "CMYK.4dbase/CMYK.4DZ"]
    require(len(nested) == 1, "original 4DZ missing")
    for name, content, _ in zip_entries(nested[0], "Project"):
        require(not name.lower().endswith(".4dm") and content[:4] not in MAGIC,
                "source or unexpected Mach-O inside opaque 4DZ")
    directory.mkdir(mode=0o700)
    for name, content, mode in entries:
        output = directory.joinpath(*PurePosixPath(name).parts)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(content)
        output.chmod(mode)
    return directory / "CMYK.4dbase"


def inventory(directory):
    require(directory.is_dir() and not directory.is_symlink(), "payload root invalid")
    result = {}
    for path in sorted(directory.rglob("*"), key=lambda item: item.relative_to(directory).as_posix()):
        require(not path.is_symlink(), "payload symlink prohibited")
        if path.is_dir():
            continue
        regular(path)
        mode = stat.S_IMODE(path.stat().st_mode)
        require(mode in (0o644, 0o755), "unexpected payload mode")
        name = path.relative_to(directory).as_posix()
        require("\\" not in name and ":" not in name, "unsafe payload path")
        result[name] = {"sha256": sha256(path), "bytes": path.stat().st_size, "mode": mode}
    require(result and "CMYK.4DZ" in result, "compiled payload missing")
    return result


def code_inventory(directory):
    found = {path.relative_to(directory).as_posix() for path in directory.rglob("*")
             if path.is_file() and path.read_bytes()[:4] in MAGIC}
    require(found == set(CODE), "unexpected or missing Mach-O object")
    result = {}
    for name in sorted(CODE):
        path = directory / name
        out, _ = run("inspect code architectures", ["/usr/bin/lipo", "-archs", str(path)])
        arches = text(out).split()
        require(len(arches) == len(set(arches)) and set(arches) == CODE[name],
                "unexpected code architectures")
        out, _ = run("inspect code UUIDs", ["/usr/bin/dwarfdump", "--uuid", str(path)])
        matches = re.findall(r"^UUID: ([0-9A-Fa-f-]{36}) \(([^)]+)\)", text(out), re.MULTILINE)
        uuids = {arch: str(uuid.UUID(value)) for value, arch in matches}
        require(set(uuids) == CODE[name] and len(matches) == len(uuids), "code UUID evidence missing")
        out, _ = run("inspect code imports", ["/usr/bin/otool", "-L", str(path)])
        imports = [line.strip().split(" (", 1)[0] for line in text(out).splitlines()
                   if " (compatibility version " in line]
        if name.startswith("Libraries/"):
            # A dylib's first install-name entry identifies itself, not an import.
            require(imports, "dylib install name missing")
            imports = imports[1:]
        require(all(item.startswith(("/usr/lib/", "/System/Library/")) for item in imports),
                "non-system dynamic dependency prohibited")
        result[name] = {"architectures": sorted(arches), "uuids": uuids, "imports": imports}
    return result


def validate_changes(before, after):
    require(set(before) == set(after), "signed payload paths changed")
    for name in before:
        require(before[name]["mode"] == after[name]["mode"], "signed payload mode changed")
        require(name in CODE or before[name] == after[name], "non-code payload bytes changed")


def select_identity(identity_text, certificates, team):
    require(isinstance(team, str) and re.fullmatch(r"[A-Z0-9]{10}", team), "invalid expected team format")
    identities = re.findall(r'^\s*\d+\)\s+([A-Fa-f0-9]{40})\s+"(Developer ID Application:[^"\n]+)"\s*$',
                            identity_text, re.MULTILINE)
    require(len(identities) == 1, "exactly one valid Developer ID Application identity required")
    fingerprint = identities[0][0].upper()
    matched = [item for item in certificates if item["sha1"].upper() == fingerprint]
    require(len(matched) == 1, "identity does not match imported certificate DER")
    certificate = matched[0]
    require(certificate["ou"] == [team], "certificate OU does not match expected team")
    require(len(certificate["cn"]) == 1 and certificate["cn"][0].startswith("Developer ID Application:")
            and certificate["developer_id"] is True, "certificate is not Developer ID Application")
    return fingerprint


def certificate_records(pem_data, scratch):
    blocks = re.findall(rb"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", pem_data, re.DOTALL)
    require(blocks, "imported public certificate missing")
    records = []
    for index, block in enumerate(blocks):
        pem = scratch / ("public-certificate-" + str(index) + ".pem")
        pem.write_bytes(block + b"\n")
        pem.chmod(0o600)
        der, _ = run("read public certificate DER", ["/usr/bin/openssl", "x509", "-in", str(pem), "-outform", "DER"])
        subject, _ = run("read public certificate subject", ["/usr/bin/openssl", "x509", "-in", str(pem),
                        "-noout", "-subject", "-nameopt", "sep_multiline,sname,utf8"])
        details, _ = run("read public certificate extensions", ["/usr/bin/openssl", "x509", "-in", str(pem),
                                                               "-noout", "-text"])
        run("check certificate expiry", ["/usr/bin/openssl", "x509", "-in", str(pem), "-noout", "-checkend", "0"])
        value = text(subject)
        records.append({"sha1": hashlib.sha1(der).hexdigest().upper(),
                        "ou": re.findall(r"^\s*OU\s*=\s*(.*?)\s*$", value, re.MULTILINE),
                        "cn": re.findall(r"^\s*CN\s*=\s*(.*?)\s*$", value, re.MULTILINE),
                        "developer_id": "1.2.840.113635.100.6.1.13" in text(details)})
    return records


@contextmanager
def signing_keychain(scratch, secrets):
    keychain = scratch / "signing.keychain-db"
    p12 = scratch / "certificate.p12"
    try:
        try:
            decoded = base64.b64decode(secrets["APPLE_DEVELOPER_ID_CERTIFICATE"], validate=True)
        except (ValueError, binascii.Error):
            raise SigningError("certificate is not strict base64") from None
        require(decoded, "empty certificate prohibited")
        p12.write_bytes(decoded)
        p12.chmod(0o600)
        run("create isolated signing keychain", ["/usr/bin/security", "create-keychain", "-p",
                                                secrets["KEYCHAIN_PASSWORD"], str(keychain)])
        run("unlock isolated signing keychain", ["/usr/bin/security", "unlock-keychain", "-p",
                                                secrets["KEYCHAIN_PASSWORD"], str(keychain)])
        run("set signing keychain timeout", ["/usr/bin/security", "set-keychain-settings", "-lut", "21600", str(keychain)])
        run("import signing certificate", ["/usr/bin/security", "import", str(p12), "-k", str(keychain),
             "-P", secrets["APPLE_DEVELOPER_ID_CERTIFICATE_PASSWORD"], "-T", "/usr/bin/codesign",
             "-T", "/usr/bin/security"])
        run("allow Apple signing partition", ["/usr/bin/security", "set-key-partition-list",
            "-S", "apple-tool:,apple:,codesign:", "-s", "-k", secrets["KEYCHAIN_PASSWORD"], str(keychain)])
        identities, _ = run("discover valid signing identity",
                            ["/usr/bin/security", "find-identity", "-v", "-p", "codesigning", str(keychain)])
        certificates, _ = run("export imported public certificates",
                              ["/usr/bin/security", "find-certificate", "-a", "-p", str(keychain)])
        fingerprint = select_identity(text(identities), certificate_records(certificates, scratch),
                                      secrets["NOTARYTOOL_TEAM_ID"])
        yield keychain, fingerprint
    finally:
        # The owner restores the journaled search list after removing this keychain.
        try:
            p12.unlink(missing_ok=True)
        finally:
            try:
                if keychain.exists():
                    run("delete isolated signing keychain", ["/usr/bin/security", "delete-keychain", str(keychain)])
            finally:
                for path in scratch.glob("public-certificate-*.pem"):
                    path.unlink()


def verify_signature(path, team, arches=None, runtime=True, certificate_sha1=None, scratch=None):
    run("strict code signature verification",
        ["/usr/bin/codesign", "--verify", "--strict", "--all-architectures", str(path)])
    for arch in sorted(arches) if arches else (None,):
        args = ["/usr/bin/codesign", "--display", "--verbose=4"]
        if arch:
            args += ["--arch", arch]
        out, err = run("inspect code signature", [*args, str(path)])
        description = text(out + err)
        require("Authority=Developer ID Application:" in description
                and re.search(r"^TeamIdentifier=" + re.escape(team) + r"$", description, re.MULTILINE)
                and re.search(r"^Timestamp=.+$", description, re.MULTILINE)
                and "Timestamp=none" not in description, "Developer ID team or secure timestamp missing")
        if runtime:
            require(re.search(r"^CodeDirectory .*flags=.*\bruntime\b", description, re.MULTILINE),
                    "hardened runtime missing")
            entitlements = ["/usr/bin/codesign", "--display", "--entitlements", ":-"]
            if arch:
                entitlements += ["--arch", arch]
            out, _ = run("inspect payload entitlements", [*entitlements, str(path)])
            if out.strip():
                try:
                    value = plistlib.loads(out)
                except (ValueError, plistlib.InvalidFileException):
                    raise SigningError("invalid code entitlements") from None
                require(value == {}, "payload entitlements prohibited")
        if certificate_sha1 is not None:
            prefix = scratch / ("signature-leaf-" + (arch or "container"))
            command = ["/usr/bin/codesign", "--display", "--extract-certificates=" + str(prefix)]
            if arch:
                command += ["--arch", arch]
            run("extract signing leaf certificate", [*command, str(path)])
            leaf = Path(str(prefix) + "0")
            regular(leaf)
            require(hashlib.sha1(leaf.read_bytes()).hexdigest().upper() == certificate_sha1,
                    "signed leaf certificate differs from imported identity")
            for item in scratch.glob(prefix.name + "*"):
                item.unlink()


def accepted_notarization(data):
    try:
        response = json.loads(text(data))
        require(isinstance(response, dict) and response.get("status") == "Accepted",
                "notarization did not explicitly return Accepted")
        identifier = str(uuid.UUID(response["id"]))
    except (ValueError, KeyError, TypeError, AttributeError):
        raise SigningError("invalid notarization response") from None
    return {"status": "Accepted", "id": identifier}


def gatekeeper_assess(path, team, kind, *, classify_non_app=False):
    require(kind in ("dmg", "cli"), "unsupported Gatekeeper assessment kind")
    policy_command = ["/usr/sbin/spctl", "--status"]
    out, err = run("inspect Gatekeeper policy", policy_command)
    require(text(out + err).strip() == "assessments enabled", "Gatekeeper policy is not explicitly enabled")
    command = ["/usr/sbin/spctl", "--assess", "--verbose=4", "--type"]
    command += ["open", "--context", "context:primary-signature"] if kind == "dmg" else ["execute"]
    command.append(str(path))
    result = capture("Gatekeeper DMG assessment" if kind == "dmg" else "Gatekeeper shipped CLI assessment",
                     command, timeout=300)
    lines = [line.strip() for line in text(result.stdout + result.stderr).splitlines() if line.strip()]
    origins = [line for line in lines if line.startswith("origin=")]
    require(len(origins) <= 1, "ambiguous Gatekeeper origin")
    if origins:
        match = re.fullmatch(r"origin=Developer ID Application: .+ \(([A-Z0-9]{10})\)", origins[0])
        require(match is not None and match[1] == team, "Gatekeeper origin team does not match signing certificate")
    non_app = str(path) + ": rejected (the code is valid but does not seem to be an app)"
    if classify_non_app and kind == "cli" and result.returncode == 3 and lines.count(non_app) == 1:
        require(set(lines) == {non_app, *origins}, "unexpected metadata alongside non-app diagnostic")
        return {"status": "failed", "applicable": False, "classification": "valid_non_app",
                "attempted": True, "exit_code": 3,
                "reason": "spctl reported valid code but not an app; direct notarized requirement is mandatory",
                "diagnostic": "the code is valid but does not seem to be an app", "command": command}
    require(result.returncode == 0, "Gatekeeper assessment rejected artifact")
    accepted = str(path) + ": accepted"
    require(lines.count(accepted) == 1, "Gatekeeper did not explicitly accept the exact artifact")
    sources = [line for line in lines if line.startswith("source=")]
    require(sources == ["source=Notarized Developer ID"], "Gatekeeper notarized Developer ID source required")
    team_lines = [line for line in lines
                  if re.match(r"^(?:teamidentifier|team-id|teamid|team_id|team)=", line, re.IGNORECASE)]
    require(all(line.split("=", 1)[1] == team for line in team_lines), "Gatekeeper reported unexpected team")
    # Fail closed on disabled/overridden policy, extra sources, or other
    # success-shaped diagnostics. Do not retain raw origin/account/team text.
    allowed = {accepted, *sources, *origins, *team_lines}
    require(all(line in allowed for line in lines), "Gatekeeper returned unexpected or bypass assessment metadata")
    return {"status": "passed", "accepted": True, "source": "Notarized Developer ID", "policy": "assessments enabled",
            "origin_present": bool(origins), "reported_team_match": True if origins or team_lines else None,
            "command": command, "policy_command": policy_command}


def apple_tool_versions():
    tools = {}
    for name, command in (
        ("macos_product", ["/usr/bin/sw_vers", "-productVersion"]),
        ("macos_build", ["/usr/bin/sw_vers", "-buildVersion"]),
        ("xcode", ["/usr/bin/xcodebuild", "-version"]),
        ("notarytool", ["/usr/bin/xcrun", "notarytool", "--version"]),
    ):
        out, err = run("identify " + name, command, timeout=60)
        version = text(out + err).strip()
        require(version and len(version) <= 4096, "Apple tool version evidence missing or oversized")
        tools[name] = {"version": version, "command": command}
    for name, path in (("spctl", "/usr/sbin/spctl"), ("codesign", "/usr/bin/codesign"),
                       ("security", "/usr/bin/security"),
                       ("hdiutil", "/usr/bin/hdiutil")):
        regular(Path(path))
        tools[name] = {"path": path, "sha256": sha256(path),
                       "version_source": "OS-bundled tool identified by macos_product/macos_build and exact binary SHA256"}
    discovery = ["/usr/bin/xcrun", "--find", "stapler"]
    out, _ = run("locate stapler", discovery, timeout=60)
    lines = text(out).splitlines()
    require(len(lines) == 1 and Path(lines[0]).is_absolute() and Path(lines[0]).name == "stapler",
            "stapler tool path is not a single absolute executable path")
    regular(Path(lines[0]))
    tools["stapler"] = {"path": lines[0], "sha256": sha256(lines[0]),
                        "discovery_command": discovery,
                        "version_source": "Xcode-bundled tool identified by xcode version and exact binary SHA256"}
    return tools


@contextmanager
def mounted(dmg, mountpoint):
    mountpoint.mkdir(mode=0o700)
    attached = False
    try:
        out, _ = run("mount notarized DMG read-only", ["/usr/bin/hdiutil", "attach", "-readonly", "-nobrowse",
                         "-plist", "-mountpoint", str(mountpoint), str(dmg)])
        attached = True
        try:
            response = plistlib.loads(out)
        except (ValueError, plistlib.InvalidFileException):
            raise SigningError("invalid DMG mount response") from None
        points = [entry.get("mount-point") for entry in response.get("system-entities", []) if entry.get("mount-point")]
        require(points == [str(mountpoint)], "unexpected DMG mount point")
        yield mountpoint
    finally:
        # Also detach if attach mounted successfully but returned malformed/failed output.
        if attached or os.path.ismount(mountpoint):
            run("detach notarized DMG", ["/usr/bin/hdiutil", "detach", str(mountpoint)], timeout=120)
        require(not os.path.ismount(mountpoint), "DMG remains mounted; manual detach required")
        mountpoint.rmdir()


def repack(directory, destination):
    entries = [(directory.name + "/" + name, (directory / name).read_bytes(), entry["mode"])
               for name, entry in inventory(directory).items()]
    write_zip(destination, entries)
    zip_entries(destination.read_bytes(), directory.name)


def production(root, tool4d, tag, expected_sha, candidate=None):
    root = Path(root).absolute()
    require(not any(path.is_symlink() for path in (root, *root.parents)), "root symlinks prohibited")
    output = root / "dist/CMYK.signing.json"
    require(not any(path.is_symlink() for path in (output, *output.parents)), "evidence symlinks prohibited")
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"schema": 1, "commit": expected_sha, "version": tag_version(tag),
              "finished": False, "result": "failed", "release_ready": False,
              "started_at": datetime.now(timezone.utc).isoformat(), "steps": {},
              "scope": "signed/notarized experimental derivative; not normal 4D application loading",
              "zip_stapling": {"status": "not_run", "reason": "ZIP cannot be stapled; identical signed payload covered by notarized DMG"},
              "library_strategy": "Sign external arm64 Mach-O dylib with Developer ID, secure timestamp and hardened runtime; no entitlements. Opaque MX64 streams/4DZ are not signed."}
    stage = "preflight"
    scratch = None
    public_replaced = False
    original = root / "dist/CMYK.zip"
    public_dmg = root / "dist/CMYK.dmg"

    def save():
        output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def passed(name, details=None):
        report["steps"][name] = {"status": "passed"}
        if details is not None:
            report["steps"][name]["details"] = details
        save()

    def cleanup_payloads():
        cleanup(root)

    save()  # Invalidate any previous success before even preflight.
    try:
        config = preflight(root, tag, expected_sha, candidate=candidate)
        stage = "presign"
        report["presign"] = validate_presign(root, config, expected_sha)
        regular(Path(tool4d))
        require(sha256(tool4d) == report["presign"]["tool4d_sha256"],
                "signed harness tool4d differs from original accepted tool")
        version_output, _ = run("identify tool4d harness", [str(Path(tool4d).absolute()), "--version"], timeout=30)
        require(text(version_output).strip(), "tool4d version evidence missing")
        report["tool4d"] = {"version": text(version_output).strip(), "sha256": sha256(tool4d)}
        report["tools"] = apple_tool_versions()
        scratch = root / "build" / ("signing-" + uuid.uuid4().hex)
        require(not any(path.is_symlink() for path in (scratch, *scratch.parents)), "scratch symlinks prohibited")
        create_cleanup_state(root, scratch)
        scratch.mkdir(parents=True, mode=0o700)
        backup = root / "build" / ("CMYK-presign-" + scratch.name.removeprefix("signing-") + ".zip")
        require(not backup.exists() and not backup.is_symlink(), "presign backup already exists")
        shutil.copyfile(original, backup)
        require(sha256(backup) == report["presign"]["artifact"]["sha256"], "presign copy changed")
        report["presign"]["retained_path"] = backup.relative_to(root).as_posix()
        # Prior public DMGs are not evidence of this attempt.
        require(not public_dmg.is_symlink(), "DMG symlink prohibited")
        public_dmg.unlink(missing_ok=True)
        stage = "payload_inventory"
        payload = extract(backup, scratch / "unsigned")
        before = inventory(payload)
        before_code = code_inventory(payload)
        for name in CODE:
            require(before[name]["sha256"] == sha256(root / "component/CMYK" / name),
                    "unsigned code differs from compiled working inputs")
        windows = root / "build/windows-x64/image-cmyk.exe"
        require(before.get("Resources/bin/Windows/image-cmyk.exe", {}).get("sha256") == sha256(windows),
                "unsigned Windows executable differs from native manifest")
        report["payload"] = {"before": before}
        secrets = {name: os.environ.get(name, "") for name in SECRETS}
        require(all(secrets.values()), "all six signing secrets must be configured")
        stage = "payload_signatures"
        original_search_list = snapshot_keychain_search_list(root)
        with signing_keychain(scratch, secrets) as (keychain, fingerprint):
            team = secrets["NOTARYTOOL_TEAM_ID"]
            report["certificate_sha1"] = fingerprint
            run("enable isolated signing keychain search",
                ["/usr/bin/security", "list-keychains", "-d", "user", "-s",
                 str(keychain), *original_search_list])
            for name in sorted(CODE):
                report["active_code_object"] = name
                run("sign payload code", ["/usr/bin/codesign", "--force", "--sign", fingerprint,
                    "--keychain", str(keychain), "--timestamp", "--options", "runtime", str(payload / name)])
                verify_signature(payload / name, team, CODE[name], certificate_sha1=fingerprint, scratch=scratch)
            after = inventory(payload)
            validate_changes(before, after)
            after_code = code_inventory(payload)
            require(before_code == after_code, "code architecture, UUID or imports changed while signing")
            report["payload"].update(after=after, code_objects=[
                {"path": name, **after_code[name], "before_sha256": before[name]["sha256"],
                 "sha256": after[name]["sha256"], "certificate_sha1": fingerprint,
                 "team_match": True, "timestamp": True, "hardened_runtime": True}
                for name in sorted(CODE)])
            report.pop("active_code_object", None)
            passed(stage)
            signed_zip = scratch / "CMYK.zip"
            repack(payload, signed_zip)
            stage = "dmg_signature"
            signed_dmg = scratch / "CMYK.dmg"
            run("create signed-payload DMG", ["/usr/bin/hdiutil", "create", "-volname", "CMYK",
                "-srcfolder", str(payload.parent), "-format", "UDZO", "-fs", "HFS+", str(signed_dmg)], timeout=600)
            run("sign DMG container", ["/usr/bin/codesign", "--force", "--sign", fingerprint,
                "--keychain", str(keychain), "--timestamp", str(signed_dmg)])
            verify_signature(signed_dmg, team, runtime=False, certificate_sha1=fingerprint, scratch=scratch)
            passed(stage)
            stage = "notarization"
            profile = "CMYK-" + uuid.uuid4().hex
            run("store isolated notary profile", ["/usr/bin/xcrun", "notarytool", "store-credentials", profile,
                "--keychain", str(keychain), "--apple-id", secrets["NOTARYTOOL_APPLE_ID"],
                "--password", secrets["NOTARYTOOL_PASSWORD"], "--team-id", team], timeout=180)
            out, _ = run("submit DMG for notarization", ["/usr/bin/xcrun", "notarytool", "submit", str(signed_dmg),
                "--keychain", str(keychain), "--keychain-profile", profile, "--wait",
                "--output-format", "json"], timeout=3600)
            passed(stage, accepted_notarization(out))
            stage = "dmg_stapling"
            run("staple DMG ticket", ["/usr/bin/xcrun", "stapler", "staple", str(signed_dmg)], timeout=300)
            run("validate stapled DMG ticket", ["/usr/bin/xcrun", "stapler", "validate", str(signed_dmg)], timeout=300)
            verify_signature(signed_dmg, team, runtime=False, certificate_sha1=fingerprint, scratch=scratch)
            passed(stage)
            stage = "dmg_assessment"
            passed(stage, gatekeeper_assess(signed_dmg, team, "dmg"))
            stage = "mounted_payload"
            with mounted(signed_dmg, scratch / "mounted") as mount:
                mounted_payload = mount / "CMYK.4dbase"
                require({p.name for p in mount.iterdir()} <= {"CMYK.4dbase", ".DS_Store", ".Trashes", ".fseventsd"}
                        and inventory(mounted_payload) == after, "mounted DMG differs from signed payload")
                for name in sorted(CODE):
                    verify_signature(mounted_payload / name, team, CODE[name],
                                     certificate_sha1=fingerprint, scratch=scratch)
            passed(stage)
            # Re-extract the exact final ZIP, not the pre-repack signing tree.
            final_payload = extract(signed_zip, scratch / "signed-final")
            require(inventory(final_payload) == after, "final signed ZIP payload mismatch")
            stage = "payload_assessment"
            notarized_objects = []
            for name in sorted(CODE):
                verify_signature(final_payload / name, team, CODE[name],
                                 certificate_sha1=fingerprint, scratch=scratch)
                command = ["/usr/bin/codesign", "--verify", "--strict", "--all-architectures",
                           "--check-notarization", "-R=notarized", str(final_payload / name)]
                run("verify payload online notarization requirement", command, timeout=300)
                notarized_objects.append({"path": name, "status": "passed", "command": command})
            # Same payload/container checks as published Text-to-SVG v0.1.1 at
            # 620cd1e: direct notarization for Mach-O, Gatekeeper for the DMG.
            cli_probe = gatekeeper_assess(final_payload / "Resources/bin/macOS/image-cmyk",
                                         team, "cli", classify_non_app=True)
            passed(stage, {"method": "strict_online_notarization", "objects": notarized_objects,
                           "cli_spctl": cli_probe, "reference": "miyako/Text-to-SVG@620cd1e5987cf93812aab4d932a8beee81dd60c7"})
            stage = "cli_smoke"
            out, err = run("signed CLI version smoke", [str(final_payload / "Resources/bin/macOS/image-cmyk"), "--version"])
            try:
                version = json.loads(text(out))
            except ValueError:
                raise SigningError("signed CLI version is not JSON") from None
            require(not err and isinstance(version, dict) and type(version.get("protocol")) is int
                    and version == {"protocol": 1, "version": config["version"]},
                    "signed CLI version contract mismatch")
            passed(stage)
            stage = "signed_compiled_harness"
            host_report = root / "build/host-status.json"
            require(not host_report.is_symlink(), "host report symlink prohibited")
            host_report.unlink(missing_ok=True)
            # No credentials are inherited by the harness or shipped executable.
            clean_env = {key: value for key, value in os.environ.items() if key not in SECRETS}
            run("signed compiled tool4d harness", [sys.executable, str(root / "scripts/test_4d.py"),
                "--root", str(root), "--tool4d", str(Path(tool4d).absolute()),
                "--component", str(final_payload)], cwd=root, timeout=600, env=clean_env)
            status = read_json(host_report)
            require(status.get("success") is True and status.get("finished") is True
                    and status.get("compiled") is True
                    and status.get("execution_scope") == "tool4d_harness"
                    and status.get("normal_app_loading") == "not_run",
                    "fresh compiled tool4d harness success and explicit scope required")
            require(inventory(final_payload) == after, "signed ZIP payload changed during harness")
            passed(stage, status)
            report["steps"][stage]["scope"] = "tool4d harness only; not normal application external component installation/loading"
            stage = "final_artifacts"
            require(sha256(root / "dist/CMYK.acceptance.json") == report["presign"]["acceptance_sha256"]
                    and sha256(root / "dist/CMYK.native-evidence.json") == report["presign"]["native_evidence_sha256"]
                    and sha256(root / "build/compile-status.json") == report["presign"]["compile_marker_sha256"]
                    and sha256(original) == report["presign"]["artifact"]["sha256"],
                    "original presign evidence or ZIP changed")
            os.replace(signed_zip, original)
            public_replaced = True
            os.replace(signed_dmg, public_dmg)
            report["artifacts"] = {"zip": artifact(original), "dmg": artifact(public_dmg)}
        stage = "cleanup"
        cleanup_payloads()
        for name, reason in {
            "normal_4d_application": "normal 4D application external component installation/loading not exercised",
            "native_intel_4d": "native Intel 4D runtime not exercised",
            "windows_4d": "Windows 4D runtime not exercised",
            "dependency_manager_install": "published dependency-manager installation not exercised",
            "windows_authenticode": "Windows executable intentionally unchanged and not Authenticode-signed",
        }.items():
            report["steps"][name] = {"status": "not_run", "reason": reason}
        report.update(finished=True, result="partial", finished_at=datetime.now(timezone.utc).isoformat())
        save()
        return report
    except (SigningError, ValueError, OSError, KeyError, TypeError, AttributeError, UnicodeError,
            subprocess.SubprocessError) as error:
        # All failure types are fatal. Never serialize exception text: third-party
        # parsers and subprocess implementations can include credential material.
        report.update(finished=False, result="failed", failed_stage=stage,
                      error=str(error) if isinstance(error, SigningError) else "stage failed; diagnostic suppressed")
        report["steps"][stage] = {"status": "failed", "reason": report["error"]}
        report.pop("artifacts", None)
        if public_replaced:
            original.unlink(missing_ok=True)
        if not public_dmg.is_symlink():
            public_dmg.unlink(missing_ok=True)
        save()
        raise SigningError("signing failed at " + stage + "; see dist/CMYK.signing.json") from None
    finally:
        # Retain presign ZIP/provenance under build. Never recursively remove a
        # potentially mounted tree, including when detach itself failed.
        if not report["finished"]:
            try:
                cleanup_payloads()
            except (OSError, SigningError):
                report["steps"]["cleanup"] = {"status": "failed", "reason": "safe staging cleanup failed; inspect private build directory"}
                save()
                raise SigningError("safe staging cleanup failed; see dist/CMYK.signing.json") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--cleanup", action="store_true", help="idempotently clean journaled CI signing resources")
    parser.add_argument("--tool4d", type=Path)
    parser.add_argument("--tag")
    parser.add_argument("--expected-sha")
    parser.add_argument("--candidate", type=Path, help="verified version-only candidate bundle directory")
    args = parser.parse_args()
    try:
        if args.cleanup:
            require(args.tool4d is None and args.tag is None and args.expected_sha is None and args.candidate is None,
                    "--cleanup accepts only --root")
            print(json.dumps({"cleanup": cleanup(args.root)}))
            return
        require(args.tool4d is not None and args.tag is not None and args.expected_sha is not None,
                "normal signing requires --tool4d, --tag and --expected-sha")
        report = production(args.root, args.tool4d, args.tag, args.expected_sha, candidate=args.candidate)
    except (SigningError, OSError, ValueError, TypeError, KeyError) as error:
        parser.exit(1, str(error) + "\n" if isinstance(error, SigningError) else "signing operation failed; diagnostic suppressed\n")
    print(json.dumps({"result": report["result"], "release_ready": False,
                      "artifacts": report["artifacts"], "evidence": "dist/CMYK.signing.json"}))


if __name__ == "__main__":
    main()
