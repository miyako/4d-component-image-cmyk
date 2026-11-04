"""Portable signing-policy tests; no Apple commands, credentials or keychains."""
from contextlib import contextmanager, redirect_stdout
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import unittest
from unittest.mock import patch
import uuid
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import sign_release as signing


SHA = "a" * 40
CERT = "B" * 40
TEAM = "AB12345678"


class Fixture(unittest.TestCase):
    def setUp(self):
        self.root = Path.cwd() / (".signing-tests-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        # Windows does not implement POSIX execute bits. Archive-mode validation
        # remains real on every host; model filesystem inventory modes on Windows.
        if os.name == "nt":
            original = stat.S_IMODE
            mode = patch.object(signing.stat, "S_IMODE",
                                side_effect=lambda value: 0o644 if stat.S_ISREG(value) else original(value))
            mode.start()
            self.addCleanup(mode.stop)

    def file(self, name, content=b"data", mode=0o644):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(mode)
        return path

    def archive(self):
        project = io.BytesIO()
        signing.write_zip(project, [("Project/DerivedData/CompiledCode/MX64 1", b"raw x86 code", 0o644),
                                    ("Project/DerivedData/CompiledCode/classes.json", b"{}", 0o644)])
        archive = self.root / "dist/CMYK.zip"
        archive.parent.mkdir(parents=True, exist_ok=True)
        signing.write_zip(archive, [
            ("CMYK.4dbase/CMYK.4DZ", project.getvalue(), 0o644),
            ("CMYK.4dbase/Resources/bin/macOS/image-cmyk", b"\xcf\xfa\xed\xfe" + b"cli", 0o755),
            ("CMYK.4dbase/Libraries/lib4d-arm64.dylib", b"\xcf\xfa\xed\xfe" + b"library", 0o755),
            ("CMYK.4dbase/Resources/bin/Windows/image-cmyk.exe", b"MZ" + b"windows", 0o755),
            ("CMYK.4dbase/Resources/Z.txt", b"Z", 0o644),
            ("CMYK.4dbase/Resources/a.txt", b"a", 0o644)])
        return archive


class PolicyTests(Fixture):
    def env(self, event="push"):
        return {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": event,
                "GITHUB_REF": "refs/tags/" + signing.TAG, "GITHUB_SHA": SHA,
                "GITHUB_REPOSITORY": "miyako/CMYK"}

    def preflight(self, env, **kwargs):
        def command(label, args, **options):
            if label == "inspect optional preflight tag":
                return ("refs/tags/" + signing.TAG + "\n").encode(), b""
            return (SHA + "\n").encode(), b""

        with patch.object(signing, "commit", return_value=SHA), \
                patch.object(signing, "run", side_effect=command), \
                patch.object(signing, "load_config", return_value={"repository": "CMYK", "cli": "image-cmyk"}):
            return signing.preflight(self.root, kwargs.get("tag", signing.TAG),
                                     kwargs.get("sha", SHA), env, kwargs.get("system", "Darwin"))

    def test_event_allowlist(self):
        self.preflight(self.env())
        manual = self.env("workflow_dispatch")
        manual["GITHUB_REF"] = "refs/heads/main"
        self.preflight(manual)
        for event in ("pull_request", "pull_request_target", "workflow_run", "schedule", ""):
            with self.subTest(event=event), self.assertRaises(signing.SigningError):
                self.preflight(self.env(event))
        manual["GITHUB_REF"] = "refs/heads/signed-notarized-rc2"
        with self.assertRaises(signing.SigningError):
            self.preflight(manual)

    def test_untagged_main_preflight_requires_dispatched_sha(self):
        env = self.env("workflow_dispatch")
        env["GITHUB_REF"] = "refs/heads/main"
        with patch.object(signing, "commit", return_value=SHA), \
                patch.object(signing, "run", return_value=(b"", b"")) as command, \
                patch.object(signing, "load_config", return_value={"repository": "CMYK", "cli": "image-cmyk"}):
            signing.preflight(self.root, signing.TAG, SHA, env, "Darwin")
            self.assertEqual(command.call_count, 1)
            self.assertEqual(command.call_args.args[0], "inspect optional preflight tag")
            env["GITHUB_SHA"] = "d" * 40
            with self.assertRaisesRegex(signing.SigningError, "exact dispatched main SHA"):
                signing.preflight(self.root, signing.TAG, SHA, env, "Darwin")

    def test_existing_tag_dispatch_may_target_an_older_verified_commit(self):
        env = self.env("workflow_dispatch")
        env["GITHUB_REF"] = "refs/heads/main"
        env["GITHUB_SHA"] = "d" * 40
        self.preflight(env)

    def test_head_tag_platform_and_version(self):
        for args in ({"sha": "c" * 40}, {"sha": "bad"}, {"tag": "v0.1.0-rc.1"}, {"system": "Windows"}):
            with self.subTest(args=args), self.assertRaises(signing.SigningError):
                self.preflight(self.env(), **args)
        wrong = self.env()
        wrong["GITHUB_SHA"] = "d" * 40
        with self.assertRaises(signing.SigningError):
            self.preflight(wrong)
        with patch.object(signing, "commit", return_value=SHA), \
                patch.object(signing, "run", return_value=(("d" * 40).encode(), b"")):
            with self.assertRaises(signing.SigningError):
                signing.preflight(self.root, signing.TAG, SHA, self.env(), "Darwin")

    def test_real_config_version_guard(self):
        # Run the existing configuration validator against a deliberately stale
        # package, without touching the actual branch's concurrently edited files.
        config = {"schema": 1, "initialized": False, "repository": "CMYK", "cli": "image-cmyk",
                  "version": "0.1.0-rc.1", "language": None}
        self.file("template.json", json.dumps(config).encode())
        self.file("package.json", json.dumps({"name": "CMYK", "version": config["version"]}).encode())
        with self.assertRaisesRegex(ValueError, "tag"):
            signing.load_config(self.root, tag=signing.TAG)

    def certificate(self, **kwargs):
        return {"sha1": CERT, "ou": [TEAM], "cn": ["Developer ID Application: Example (" + TEAM + ")"],
                "developer_id": True, **kwargs}

    def test_identity_matches_actual_certificate_not_display_team(self):
        listing = '  1) ' + CERT + ' "Developer ID Application: Example (' + TEAM + ')"\n  1 valid identities found\n'
        self.assertEqual(signing.select_identity(listing, [self.certificate()], TEAM), CERT)
        for cert in (self.certificate(ou=["WRONG12345"]), self.certificate(ou=[TEAM, TEAM]),
                     self.certificate(sha1="C" * 40), self.certificate(developer_id=False),
                     self.certificate(cn=["Apple Development: Example"])):
            with self.subTest(cert=cert), self.assertRaises(signing.SigningError):
                signing.select_identity(listing, [cert], TEAM)
        with self.assertRaises(signing.SigningError):
            signing.select_identity(listing + listing, [self.certificate()], TEAM)

    def test_notary_requires_explicit_accepted_and_uuid(self):
        identifier = str(uuid.uuid4())
        self.assertEqual(signing.accepted_notarization(json.dumps({"status": "Accepted", "id": identifier}).encode()),
                         {"status": "Accepted", "id": identifier})
        for value in ({"status": "In Progress", "id": identifier}, {"status": "Invalid", "id": identifier},
                      {"status": "accepted", "id": identifier}, {"status": "Accepted", "id": "bogus"},
                      {}, [], None):
            with self.subTest(value=value), self.assertRaises(signing.SigningError):
                signing.accepted_notarization(json.dumps(value).encode())


class PayloadTests(Fixture):
    def test_inventory_sorted_posix_and_nested_archive_preserved(self):
        archive = self.archive()
        payload = signing.extract(archive, self.root / "extracted")
        before = signing.inventory(payload)
        self.assertEqual(list(before), sorted(before))
        self.assertTrue(all("\\" not in name for name in before))
        repacked = self.root / "repacked.zip"
        signing.repack(payload, repacked)
        restored = signing.extract(repacked, self.root / "restored")
        self.assertEqual(signing.inventory(restored), before)
        self.assertEqual((payload / "CMYK.4DZ").read_bytes(), (restored / "CMYK.4DZ").read_bytes())
        entries = signing.zip_entries(repacked.read_bytes(), "CMYK.4dbase")
        self.assertIn(("CMYK.4dbase/Resources/bin/macOS/image-cmyk",
                       b"\xcf\xfa\xed\xfecli", before["Resources/bin/macOS/image-cmyk"]["mode"]), entries)

    def test_only_exact_code_objects_may_change_bytes(self):
        payload = signing.extract(self.archive(), self.root / "extracted")
        before = signing.inventory(payload)
        for name in signing.CODE:
            (payload / name).write_bytes((payload / name).read_bytes() + b"signed")
        after = signing.inventory(payload)
        signing.validate_changes(before, after)
        for name in ("CMYK.4DZ", "Resources/bin/Windows/image-cmyk.exe"):
            modified = copy.deepcopy(after)
            modified[name]["sha256"] = "f" * 64
            with self.subTest(name=name), self.assertRaises(signing.SigningError):
                signing.validate_changes(before, modified)
        modified = copy.deepcopy(after)
        modified["new"] = modified["CMYK.4DZ"]
        with self.assertRaises(signing.SigningError):
            signing.validate_changes(before, modified)
        modified = copy.deepcopy(after)
        modified[next(iter(signing.CODE))]["mode"] = 0o600
        with self.assertRaises(signing.SigningError):
            signing.validate_changes(before, modified)

    def test_unexpected_macho_rejected_before_commands(self):
        payload = signing.extract(self.archive(), self.root / "extracted")
        (payload / "rogue").write_bytes(b"\xcf\xfa\xed\xfeoops")
        with patch.object(signing, "run") as run:
            with self.assertRaisesRegex(signing.SigningError, "unexpected"):
                signing.code_inventory(payload)
            run.assert_not_called()

    def test_code_architectures_uuids_and_dependency_allowlist(self):
        payload = signing.extract(self.archive(), self.root / "extracted")

        def command(label, args, **kwargs):
            name = Path(args[-1]).relative_to(payload).as_posix()
            arches = sorted(signing.CODE[name])
            if label == "inspect code architectures":
                return " ".join(arches).encode(), b""
            if label == "inspect code UUIDs":
                return "\n".join(f"UUID: {uuid.UUID(int=index + 1)} ({arch}) object"
                                 for index, arch in enumerate(arches)).encode(), b""
            if label == "inspect code imports":
                own_id = "\t@rpath/lib4d-arm64.dylib (compatibility version 1.0.0, current version 1.0.0)\n"
                system = "\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0, current version 1.0.0)\n"
                return ((own_id if name.startswith("Libraries/") else "") + system).encode(), b""
            self.fail(label)

        with patch.object(signing, "run", side_effect=command):
            result = signing.code_inventory(payload)
        self.assertEqual(set(result), set(signing.CODE))
        for name, record in result.items():
            self.assertEqual(set(record["architectures"]), signing.CODE[name])
            self.assertEqual(set(record["uuids"]), signing.CODE[name])
            self.assertEqual(record["imports"], ["/usr/lib/libSystem.B.dylib"])

        def bad_import(label, args, **kwargs):
            if label == "inspect code imports":
                return (b"\t@rpath/lib4d-arm64.dylib (compatibility version 1.0.0, current version 1.0.0)\n"
                        b"\t/unsafe/lib4d-arm64.dylib (compatibility version 1.0.0, current version 1.0.0)\n"), b""
            return command(label, args, **kwargs)

        with patch.object(signing, "run", side_effect=bad_import):
            with self.assertRaisesRegex(signing.SigningError, "non-system"):
                signing.code_inventory(payload)

    def test_inventory_symlink_rejected_portably(self):
        payload = signing.extract(self.archive(), self.root / "extracted")
        original = Path.is_symlink
        with patch.object(Path, "is_symlink", lambda path: path.name == "Z.txt" or original(path)):
            with self.assertRaisesRegex(signing.SigningError, "symlink"):
                signing.inventory(payload)

    def test_zip_unsafe_entries_and_missing_directory_records(self):
        for name, mode in (("../escape", stat.S_IFREG | 0o644),
                           ("CMYK.4dbase/link", stat.S_IFLNK | 0o755),
                           ("CMYK.4dbase/FIFO", stat.S_IFIFO | 0o644),
                           ("CMYK.4dbase/a\\b", stat.S_IFREG | 0o644),
                           ("CMYK.4dbase/./x", stat.S_IFREG | 0o644),
                           ("CMYK.4dbase/CMYK.4DZ", stat.S_IFREG | 0o644)):
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                entry = zipfile.ZipInfo(name)
                entry.create_system = 3
                entry.external_attr = mode << 16
                archive.writestr(entry, b"payload")
            with self.subTest(name=name), self.assertRaises(signing.SigningError):
                signing.zip_entries(buffer.getvalue(), "CMYK.4dbase")


class PresignTests(Fixture):
    def evidence(self):
        archive = self.archive()
        config = {"version": "0.1.0-rc.2"}
        compiler = {"success": True, "errors": [], "targets": ["x86_64_generic", "arm64_macOS_lib"],
                    "input_sha256": "1" * 64, "output_sha256": "2" * 64}
        steps = {name: {"status": "passed"} for name in
                 ("unit", "icons", "dependencies", "assembly", "source_host", "compile", "package", "compiled_host")}
        steps["compile"]["details"] = compiler
        for name, mode in (("source_host", False), ("compiled_host", True)):
            steps[name]["details"] = {"success": True, "finished": True, "compiled": mode,
                                      "execution_scope": "tool4d_harness", "normal_app_loading": "not_run"}
        acceptance = {"schema": 1, "configuration": config, "commit": SHA, "finished": True,
                      "result": "partial", "release_ready": False, "artifact": signing.artifact(archive),
                      "steps": steps, "tool4d": {"binary_sha256": "4" * 64}}
        manifests = {target: {"target": target, "commit": SHA, "source_sha256": "3" * 64,
                              "execution": "passed"} for target in signing.TARGETS}
        native = {"schema": 1, "commit": SHA, "source_sha256": "3" * 64, "targets": manifests}
        self.file("dist/CMYK.acceptance.json", json.dumps(acceptance).encode())
        self.file("dist/CMYK.native-evidence.json", json.dumps(native).encode())
        self.file("build/compile-status.json", json.dumps(compiler).encode())
        return config, acceptance, native, manifests

    def test_strict_original_hashes_compilation_and_native_revision(self):
        config, acceptance, native, manifests = self.evidence()
        with patch.object(signing, "validate_compile") as compile_check, \
                patch.object(signing, "presign_assets") as presign_check, \
                patch.object(signing, "source_digest", return_value="3" * 64), \
                patch.object(signing, "verify", side_effect=lambda root, target, binary: manifests[target]):
            report = signing.validate_presign(self.root, config, SHA)
            presign_check.assert_called_once_with(self.root, SHA)
            compile_check.assert_called_once_with(self.root, self.root / "component/CMYK", "CMYK")
            self.assertEqual(report["acceptance_sha256"], signing.sha256(self.root / "dist/CMYK.acceptance.json"))
            self.assertEqual(report["native_evidence_sha256"], signing.sha256(self.root / "dist/CMYK.native-evidence.json"))
            for label, change in (
                ("zip hash", lambda a, n: a["artifact"].update(sha256="0" * 64)),
                ("zip bytes", lambda a, n: a["artifact"].update(bytes=True)),
                ("acceptance commit", lambda a, n: a.update(commit="f" * 40)),
                ("compiled mode", lambda a, n: a["steps"]["compiled_host"]["details"].update(compiled=False)),
                ("harness scope", lambda a, n: a["steps"]["compiled_host"]["details"].update(execution_scope="normal_app")),
                ("compile errors", lambda a, n: a["steps"]["compile"]["details"].update(errors=["failure"])),
                ("native commit", lambda a, n: n.update(commit="f" * 40)),
                ("native source", lambda a, n: n.update(source_sha256="f" * 64)),
                ("native missing", lambda a, n: n["targets"].pop("windows-x64")),
            ):
                bad_acceptance, bad_native = copy.deepcopy(acceptance), copy.deepcopy(native)
                change(bad_acceptance, bad_native)
                self.file("dist/CMYK.acceptance.json", json.dumps(bad_acceptance).encode())
                self.file("dist/CMYK.native-evidence.json", json.dumps(bad_native).encode())
                with self.subTest(label=label), self.assertRaises(signing.SigningError):
                    signing.validate_presign(self.root, config, SHA)


class CommandTests(Fixture):
    def test_sensitive_command_errors_are_sanitized(self):
        secret = "PRIVATE-NOTARY-PASSWORD"
        errors = [
            subprocess.TimeoutExpired(["secret-command", secret], 1, output=secret, stderr=secret),
            OSError(secret)]
        for error in errors:
            with self.subTest(error=type(error).__name__), \
                    patch.object(signing.subprocess, "run", side_effect=error):
                with self.assertRaises(signing.SigningError) as raised:
                    signing.run("fixed label", ["tool", secret])
                self.assertNotIn(secret, str(raised.exception))
        with patch.object(signing.subprocess, "run", return_value=subprocess.CompletedProcess(
                ["tool", secret], 1, secret.encode(), secret.encode())):
            with self.assertRaisesRegex(signing.SigningError, "^fixed label: nonzero exit$"):
                signing.run("fixed label", ["tool", secret])

    def test_subprocess_does_not_inherit_credentials(self):
        with patch.dict(os.environ, {"NOTARYTOOL_PASSWORD": "private", "SAFE": "yes"}), \
                patch.object(signing.subprocess, "run",
                             return_value=subprocess.CompletedProcess([], 0, b"", b"")) as process:
            signing.run("inspect", ["tool"])
            self.assertNotIn("NOTARYTOOL_PASSWORD", process.call_args.kwargs["env"])
            self.assertEqual(process.call_args.kwargs["env"]["SAFE"], "yes")

    def test_cleanup_on_import_failure_and_delete_failure(self):
        secrets = {name: "placeholder" for name in signing.SECRETS}
        secrets["APPLE_DEVELOPER_ID_CERTIFICATE"] = "Y2VydA=="
        for failing in ("import signing certificate", "delete isolated signing keychain"):
            commands = []

            def command(label, args, **kwargs):
                commands.append((label, args))
                if label == "create isolated signing keychain":
                    Path(args[-1]).write_bytes(b"keychain")
                if label == failing:
                    raise signing.SigningError("injected command failure")
                if label == "delete isolated signing keychain":
                    Path(args[-1]).unlink()
                if label == "discover valid signing identity":
                    return b'  1) ' + CERT.encode() + b' "Developer ID Application: Example"\n', b""
                return b"", b""

            with self.subTest(failing=failing), patch.object(signing, "run", side_effect=command), \
                    patch.object(signing, "certificate_records", return_value=[
                        {"sha1": CERT, "ou": [TEAM], "cn": ["Developer ID Application: Example"], "developer_id": True}]):
                secrets["NOTARYTOOL_TEAM_ID"] = TEAM
                with self.assertRaises(signing.SigningError):
                    with signing.signing_keychain(self.root, secrets):
                        pass
                self.assertFalse((self.root / "certificate.p12").exists())
                self.assertEqual((self.root / "signing.keychain-db").exists(),
                                 failing == "delete isolated signing keychain")
                self.assertIn("delete isolated signing keychain", [label for label, _ in commands])
                self.assertFalse(any("list-keychains" in args or "default-keychain" in args for _, args in commands))


class CleanupTests(Fixture):
    def state(self):
        scratch = self.root / "build" / ("signing-" + "a" * 32)
        signing.create_cleanup_state(self.root, scratch)
        scratch.mkdir()
        return scratch, self.root / "build/signing-state.json"

    def test_no_state_is_idempotent_and_runs_no_commands(self):
        with patch.object(signing, "run") as command:
            self.assertEqual(signing.cleanup(self.root), {"status": "not_needed"})
            self.assertEqual(signing.cleanup(self.root), {"status": "not_needed"})
            command.assert_not_called()

    def test_cleanup_cli_requires_no_signing_arguments(self):
        output = io.StringIO()
        with patch.object(sys, "argv", ["sign_release.py", "--root", str(self.root), "--cleanup"]), \
                patch.object(signing, "production") as production, redirect_stdout(output):
            signing.main()
            production.assert_not_called()
        self.assertEqual(json.loads(output.getvalue()), {"cleanup": {"status": "not_needed"}})

    def test_cleanup_removes_only_created_resources_preserves_presign(self):
        scratch, journal = self.state()
        p12 = scratch / "certificate.p12"
        p12.write_bytes(b"synthetic certificate")
        keychain = scratch / "signing.keychain-db"
        keychain.write_bytes(b"synthetic keychain")
        mount = scratch / "mounted"
        mount.mkdir()
        backup = self.file("build/CMYK-presign-retained.zip", b"original")
        active = {mount}
        calls = []

        def command(label, args, **kwargs):
            calls.append(label)
            if label == "delete journaled signing keychain":
                keychain.unlink()
            if label == "detach journaled signing mount":
                active.remove(mount)
            return b"", b""

        state = json.loads(journal.read_text())
        state.update(search_list_changed=True, original_search_list=[str(self.root / "original.keychain-db")])
        journal.write_text(json.dumps(state))
        with patch.object(signing.os.path, "ismount", side_effect=lambda path: Path(path) in active), \
                patch.object(signing, "run", side_effect=command):
            self.assertEqual(signing.cleanup(self.root), {"status": "passed"})
        self.assertEqual(calls, ["delete journaled signing keychain", "detach journaled signing mount",
                                 "restore original keychain search list"])
        self.assertFalse(scratch.exists())
        self.assertFalse(journal.exists())
        self.assertEqual(backup.read_bytes(), b"original")
        self.assertEqual(signing.cleanup(self.root), {"status": "not_needed"})

    def test_active_keychain_failure_propagates_and_keeps_retry_state(self):
        scratch, journal = self.state()
        p12 = scratch / "certificate.p12"
        p12.write_bytes(b"synthetic certificate")
        keychain = scratch / "signing.keychain-db"
        keychain.write_bytes(b"synthetic keychain")
        with patch.object(signing, "run", side_effect=signing.SigningError("fixed failure")):
            with self.assertRaisesRegex(signing.SigningError, "keychain removal failed"):
                signing.cleanup(self.root)
        self.assertFalse(p12.exists())
        self.assertTrue(journal.exists())
        self.assertTrue(keychain.exists())

    def test_active_mount_failure_never_recursively_deletes_mount(self):
        scratch, journal = self.state()
        mount = scratch / "mounted"
        mount.mkdir()
        with patch.object(signing.os.path, "ismount", side_effect=lambda path: Path(path) == mount), \
                patch.object(signing, "run", side_effect=signing.SigningError("fixed failure")), \
                patch.object(signing.shutil, "rmtree") as remove:
            with self.assertRaisesRegex(signing.SigningError, "mount detach failed"):
                signing.cleanup(self.root)
            remove.assert_not_called()
        self.assertTrue(journal.exists())

    def test_unexpected_nested_mount_never_deleted(self):
        scratch, _ = self.state()
        unknown = scratch / "unknown-mount"
        unknown.mkdir()
        with patch.object(signing.os.path, "ismount", side_effect=lambda path: Path(path) == unknown), \
                patch.object(signing.shutil, "rmtree") as remove:
            with self.assertRaisesRegex(signing.SigningError, "nested cleanup"):
                signing.cleanup(self.root)
            remove.assert_not_called()

    def test_search_list_restore_failure_propagates_and_remains_retryable(self):
        scratch, journal = self.state()
        state = json.loads(journal.read_text())
        state.update(search_list_changed=True, original_search_list=[str(self.root / "original.keychain-db")])
        journal.write_text(json.dumps(state))
        with patch.object(signing, "run", side_effect=signing.SigningError("fixed failure")), \
                patch.object(signing.shutil, "rmtree") as remove:
            with self.assertRaisesRegex(signing.SigningError, "search-list restoration failed"):
                signing.cleanup(self.root)
            remove.assert_not_called()
        self.assertTrue(journal.exists())
        self.assertTrue(scratch.exists())

    def test_untrusted_journal_paths_or_extra_fields_rejected_without_commands(self):
        scratch, journal = self.state()
        valid = json.loads(journal.read_text())
        variants = [
            {**valid, "staging": "../outside"},
            {**valid, "keychain": "build/elsewhere.keychain-db"},
            {**valid, "mount": str(self.root / "other")},
            {**valid, "p12": valid["p12"] + "/../other"},
            {**valid, "unknown": "not allowed"},
            {**valid, "search_list_changed": "yes"},
            {**valid, "search_list_changed": True, "original_search_list": ["-unsafe"]},
        ]
        for state in variants:
            journal.write_text(json.dumps(state))
            with self.subTest(state=state), patch.object(signing, "run") as command:
                with self.assertRaises(signing.SigningError):
                    signing.cleanup(self.root)
                command.assert_not_called()
            self.assertTrue(scratch.exists())

    def test_journal_and_target_symlinks_are_rejected_portably(self):
        scratch, journal = self.state()
        original = Path.is_symlink
        for target in (journal, scratch):
            with self.subTest(target=target), \
                    patch.object(Path, "is_symlink", lambda path: path == target or original(path)), \
                    patch.object(signing, "run") as command:
                with self.assertRaisesRegex(signing.SigningError, "symlink"):
                    signing.cleanup(self.root)
                command.assert_not_called()


class SignatureTests(Fixture):
    def test_detach_on_mount_validation_failure(self):
        mount = self.root / "mounted"
        calls = []

        def command(label, args, **kwargs):
            calls.append(label)
            return b"not plist", b""

        with patch.object(signing, "run", side_effect=command):
            with self.assertRaises(signing.SigningError):
                with signing.mounted(self.root / "fake.dmg", mount):
                    self.fail("must reject bad mount response")
        self.assertIn("detach notarized DMG", calls)
        self.assertFalse(mount.exists())

    def test_each_architecture_signature_checks_and_leaf_binding(self):
        code = self.file("binary")
        certificate = b"public certificate DER"
        fingerprint = hashlib.sha1(certificate).hexdigest().upper()
        inspected = []

        def command(label, args, **kwargs):
            if label == "inspect code signature":
                inspected.append(args[args.index("--arch") + 1])
                return b"", (f"Authority=Developer ID Application: Example\nTeamIdentifier={TEAM}\n"
                              "Timestamp=Nov 4, 2026\nCodeDirectory v=20500 flags=0x10000(runtime)\n").encode()
            if label == "extract signing leaf certificate":
                prefix = args[args.index("--extract-certificates") + 1]
                Path(prefix + "0").write_bytes(certificate)
            return b"", b""

        with patch.object(signing, "run", side_effect=command):
            signing.verify_signature(code, TEAM, {"x86_64", "arm64"},
                                     certificate_sha1=fingerprint, scratch=self.root)
        self.assertEqual(inspected, ["arm64", "x86_64"])
        self.assertFalse(list(self.root.glob("signature-leaf-*")))
        with patch.object(signing, "run", return_value=(b"", b"Authority=Apple Development\n")):
            with self.assertRaises(signing.SigningError):
                signing.verify_signature(code, TEAM, {"arm64"})

    def test_certificate_ou_is_parsed_from_public_certificate_subject(self):
        public_pem = b"-----BEGIN CERTIFICATE-----\npublic\n-----END CERTIFICATE-----"

        def command(label, args, **kwargs):
            if label == "read public certificate DER":
                return b"DER", b""
            if label == "read public certificate subject":
                return b"subject=\n    OU=WRONG12345\n    CN=Developer ID Application: Example (AB12345678)\n", b""
            if label == "read public certificate extensions":
                return b"1.2.840.113635.100.6.1.13: critical", b""
            return b"", b""

        with patch.object(signing, "run", side_effect=command):
            records = signing.certificate_records(public_pem, self.root)
        self.assertEqual(records[0]["ou"], ["WRONG12345"])
        listing = '  1) ' + records[0]["sha1"] + ' "Developer ID Application: Example (' + TEAM + ')"\n'
        with self.assertRaisesRegex(signing.SigningError, "certificate OU"):
            signing.select_identity(listing, records, TEAM)

    def test_broad_entitlements_fail_closed(self):
        import plistlib

        def command(label, args, **kwargs):
            if label == "inspect code signature":
                return b"", (f"Authority=Developer ID Application: Example\nTeamIdentifier={TEAM}\n"
                              "Timestamp=Nov 4, 2026\nCodeDirectory v=20500 flags=0x10000(runtime)\n").encode()
            if label == "inspect payload entitlements":
                return plistlib.dumps({"com.apple.security.cs.disable-library-validation": True}), b""
            return b"", b""

        with patch.object(signing, "run", side_effect=command):
            with self.assertRaisesRegex(signing.SigningError, "entitlements prohibited"):
                signing.verify_signature(self.root / "code", TEAM, {"arm64"})


class GatekeeperTests(Fixture):
    def assess(self, output, policy=b"assessments enabled\n", kind="dmg"):
        path = self.root / ("CMYK.dmg" if kind == "dmg" else "image-cmyk")

        def command(label, args, **kwargs):
            if label == "inspect Gatekeeper policy":
                return policy, b""
            return b"", output.replace(b"$PATH", str(path).encode())

        with patch.object(signing, "run", side_effect=command) as commands:
            result = signing.gatekeeper_assess(path, TEAM, kind)
        return result, commands.call_args_list

    def test_notarized_acceptance_source_team_and_commands(self):
        output = ("$PATH: accepted\nsource=Notarized Developer ID\n"
                  "origin=Developer ID Application: Example (" + TEAM + ")\n").encode()
        for kind in ("dmg", "cli"):
            result, calls = self.assess(output, kind=kind)
            self.assertTrue(result["accepted"])
            self.assertEqual(result["source"], "Notarized Developer ID")
            self.assertTrue(result["reported_team_match"])
            self.assertIn("--verbose=4", result["command"])
            self.assertIn("open" if kind == "dmg" else "execute", result["command"])
            self.assertEqual(result["policy_command"], ["/usr/sbin/spctl", "--status"])
            self.assertNotIn(TEAM, json.dumps(result))
            self.assertNotIn("Example", json.dumps(result))
            self.assertFalse(any("--master-enable" in call.args[1] or "--master-disable" in call.args[1]
                                 for call in calls))
        result, _ = self.assess(b"$PATH: accepted\nsource=Notarized Developer ID\n")
        self.assertIsNone(result["reported_team_match"])

    def test_success_exit_does_not_waive_disabled_or_bypassed_policy(self):
        good = b"$PATH: accepted\nsource=Notarized Developer ID\n"
        for policy in (b"assessments disabled\n", b"assessments enabled (override)\n", b"", b"policy disabled"):
            with self.subTest(policy=policy), self.assertRaisesRegex(signing.SigningError, "policy"):
                self.assess(good, policy=policy)
        bad_outputs = (
            b"",
            b"$PATH: accepted\n",
            b"$PATH: accepted\nsource=Developer ID\n",
            b"$PATH: accepted\nsource=Unnotarized Developer ID\n",
            b"$PATH: accepted (override)\nsource=Notarized Developer ID\n",
            good + b"override=security disabled\n",
            good + b"assessment bypassed\n",
            good + b"source=Developer ID\n",
            good + b"origin=Developer ID Application: Example (WRONG12345)\n",
            good + b"origin=Apple Development: Example (AB12345678)\n",
            good + b"team-id=WRONG12345\n",
        )
        for output in bad_outputs:
            with self.subTest(output=output), self.assertRaises(signing.SigningError):
                self.assess(output)

    def test_tool_versions_record_actual_commands_and_os_tool_hashes(self):
        def result(label, args, **kwargs):
            if label == "locate stapler":
                return (str(self.root / "stapler") + "\n").encode(), b""
            return b"actual tool version\n", b""

        with patch.object(signing, "run", side_effect=result) as command, \
                patch.object(signing, "regular"), patch.object(signing, "sha256", return_value="a" * 64):
            tools = signing.apple_tool_versions()
        for name in ("macos_product", "macos_build", "xcode", "notarytool"):
            self.assertEqual(tools[name]["version"], "actual tool version")
            self.assertIsInstance(tools[name]["command"], list)
        self.assertEqual(command.call_count, 5)
        for name in ("spctl", "codesign", "security", "hdiutil"):
            self.assertEqual(tools[name]["sha256"], "a" * 64)
            self.assertIn("macos_product/macos_build", tools[name]["version_source"])
        self.assertEqual(tools["stapler"]["sha256"], "a" * 64)
        self.assertEqual(tools["stapler"]["discovery_command"], ["/usr/bin/xcrun", "--find", "stapler"])
        commands = [call.args[1] for call in command.call_args_list]
        self.assertEqual([args for args in commands if "--version" in args],
                         [["/usr/bin/xcrun", "notarytool", "--version"]])


class PipelineTests(Fixture):
    def test_mocked_pipeline_final_bytes_evidence_and_scoped_harness(self):
        archive = self.archive()
        acceptance = self.file("dist/CMYK.acceptance.json", b'{"unsigned":"acceptance"}')
        native = self.file("dist/CMYK.native-evidence.json", b'{"unsigned":"native"}')
        tool = self.file("tool4d")
        presign = {"artifact": signing.artifact(archive), "acceptance_sha256": signing.sha256(acceptance),
                   "native_evidence_sha256": signing.sha256(native), "tool4d_sha256": signing.sha256(tool)}
        marker = self.file("build/compile-status.json", b"original compile marker")
        presign["compile_marker_sha256"] = signing.sha256(marker)
        original_bytes = archive.read_bytes()
        unsigned = signing.extract(archive, self.root / "initial")
        self.file("build/windows-x64/image-cmyk.exe",
                  (unsigned / "Resources/bin/Windows/image-cmyk.exe").read_bytes())
        for name in signing.CODE:
            self.file("component/CMYK/" + name, (unsigned / name).read_bytes())
        shutil.rmtree(unsigned.parent)
        config = {"version": "0.1.0-rc.2"}
        certificate = {name: "placeholder" for name in signing.SECRETS}
        certificate["NOTARYTOOL_TEAM_ID"] = TEAM
        signed_location = []
        commands = []

        @contextmanager
        def keychain(scratch, secrets):
            yield scratch / "keychain", CERT

        @contextmanager
        def mount(dmg, mountpoint):
            mountpoint.mkdir()
            shutil.copytree(signed_location[0], mountpoint / "CMYK.4dbase")
            try:
                yield mountpoint
            finally:
                shutil.rmtree(mountpoint)

        def command(label, args, **kwargs):
            commands.append((label, args, kwargs))
            if label == "identify tool4d harness":
                return b"tool4d 20.7 build 101905\n", b""
            if label == "inspect Gatekeeper policy":
                return b"assessments enabled\n", b""
            if label in ("Gatekeeper DMG assessment", "Gatekeeper shipped CLI assessment"):
                return b"", (str(args[-1]) + ": accepted\nsource=Notarized Developer ID\n"
                             "origin=Developer ID Application: Example (" + TEAM + ")\n").encode()
            if label == "sign payload code":
                path = Path(args[-1])
                path.write_bytes(path.read_bytes() + b"signed")
                signed_location[:] = [path.parents[3] if path.name == "image-cmyk" else path.parents[1]]
            if label == "create signed-payload DMG":
                Path(args[-1]).write_bytes(b"signed DMG")
            if label == "submit DMG for notarization":
                return json.dumps({"status": "Accepted", "id": str(uuid.uuid4())}).encode(), b""
            if label == "signed CLI version smoke":
                return b'{"protocol":1,"version":"0.1.0-rc.2"}\n', b""
            if label == "signed compiled tool4d harness":
                self.file("build/host-status.json", json.dumps({
                    "success": True, "finished": True, "compiled": True,
                    "execution_scope": "tool4d_harness", "normal_app_loading": "not_run"}).encode())
            return b"", b""

        code = {name: {"architectures": sorted(arches), "uuids": {}, "imports": []}
                for name, arches in signing.CODE.items()}
        with patch.dict(os.environ, certificate), \
                patch.object(signing, "preflight", return_value=config), \
                patch.object(signing, "validate_presign", return_value=presign), \
                patch.object(signing, "apple_tool_versions", return_value={"notarytool": {"version": "mocked"}}), \
                patch.object(signing, "code_inventory", return_value=code), \
                patch.object(signing, "signing_keychain", side_effect=keychain), \
                patch.object(signing, "verify_signature"), \
                patch.object(signing, "mounted", side_effect=mount), \
                patch.object(signing, "run", side_effect=command):
            report = signing.production(self.root, tool, signing.TAG, SHA)
        self.assertTrue(report["finished"])
        self.assertEqual(report["result"], "partial")
        self.assertFalse(report["release_ready"])
        self.assertEqual(report["presign"]["artifact"]["sha256"], hashlib.sha256(original_bytes).hexdigest())
        self.assertNotEqual(report["artifacts"]["zip"]["sha256"], report["presign"]["artifact"]["sha256"])
        self.assertEqual(signing.sha256(archive), report["artifacts"]["zip"]["sha256"])
        self.assertEqual(signing.sha256(acceptance), presign["acceptance_sha256"])
        self.assertEqual(signing.sha256(native), presign["native_evidence_sha256"])
        self.assertEqual((self.root / report["presign"]["retained_path"]).read_bytes(), original_bytes)
        signing.validate_changes(report["payload"]["before"], report["payload"]["after"])
        serialized = json.loads(json.dumps(report))
        for phase in ("before", "after"):
            self.assertTrue(all(type(item["mode"]) is int for item in serialized["payload"][phase].values()))
        self.assertEqual({item["path"] for item in serialized["payload"]["code_objects"]}, set(signing.CODE))
        for item in serialized["payload"]["code_objects"]:
            self.assertIsInstance(item["architectures"], list)
            self.assertTrue(all(type(architecture) is str for architecture in item["architectures"]))
        required = ("payload_signatures", "signed_compiled_harness", "dmg_signature", "notarization",
                    "dmg_stapling", "dmg_assessment", "payload_assessment", "mounted_payload", "cli_smoke")
        self.assertTrue(all(report["steps"][name]["status"] == "passed" for name in required))
        self.assertEqual(report["steps"]["normal_4d_application"]["status"], "not_run")
        harness = next(call for call in commands if call[0] == "signed compiled tool4d harness")
        self.assertTrue(any("signed-final" in arg for arg in harness[1]))
        self.assertNotIn("NOTARYTOOL_PASSWORD", harness[2]["env"])

    def test_failed_attempt_replaces_stale_success_and_suppresses_error(self):
        self.file("dist/CMYK.signing.json", b'{"finished":true,"result":"partial"}')
        self.file("dist/CMYK.dmg", b"stale signed artifact")
        with patch.object(signing, "preflight", side_effect=ValueError("PRIVATE-secret")):
            with self.assertRaises(signing.SigningError) as error:
                signing.production(self.root, self.root / "tool", signing.TAG, SHA)
        report = json.loads((self.root / "dist/CMYK.signing.json").read_text())
        self.assertFalse(report["finished"])
        self.assertEqual(report["result"], "failed")
        self.assertEqual(report["failed_stage"], "preflight")
        self.assertNotIn("PRIVATE-secret", str(error.exception))
        self.assertNotIn("PRIVATE-secret", json.dumps(report))
        self.assertFalse((self.root / "dist/CMYK.dmg").exists())


if __name__ == "__main__":
    unittest.main()
