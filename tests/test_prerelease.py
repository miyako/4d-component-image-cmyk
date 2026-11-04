"""Publication guards use synthetic evidence; tests never invoke GitHub."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import publish_prerelease as publisher


class PrereleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix=".prerelease-test-", dir=ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.dist = self.root / "dist"
        self.dist.mkdir()
        self.sha = "a" * 40
        self.digest = "b" * 64
        self.config = {"repository": "CMYK", "version": "0.1.0-rc.2", "cli": "image-cmyk"}
        (self.dist / "CMYK.zip").write_bytes(b"synthetic archive bytes")
        (self.dist / "CMYK.dmg").write_bytes(b"synthetic signed disk image")
        self.report = {
            "configuration": self.config, "commit": self.sha, "finished": True,
            "result": "partial", "release_ready": False,
            "artifact": {"path": "/runner/dist/CMYK.zip", "bytes": 23,
                         "sha256": hashlib.sha256(b"synthetic archive bytes").hexdigest()},
            "steps": {name: {"status": "passed"} for name in
                      ("unit", "icons", "dependencies", "assembly", "source_host",
                       "compile", "package", "compiled_host")},
        }
        for name, compiled in (("source_host", False), ("compiled_host", True)):
            self.report["steps"][name]["details"] = {
                "success": True, "finished": True, "compiled": compiled,
                "execution_scope": "tool4d_harness", "normal_app_loading": "not_run"}
        self.report["steps"]["compile"]["details"] = {
            "success": True, "errors": [], "targets": ["x86_64_generic", "arm64_macOS_lib"],
            "input_sha256": "c" * 64, "output_sha256": "d" * 64}
        self.native = {"schema": 1, "commit": self.sha, "source_sha256": self.digest,
                       "targets": {target: {"target": target, "commit": self.sha,
                                           "source_sha256": self.digest, "execution": "passed"}
                                   for target in publisher.TARGETS}}
        certificate = "E" * 40
        before = {
            "CMYK.4DZ": {"sha256": "1" * 64, "bytes": 100, "mode": 0o644},
            "Libraries/lib4d-arm64.dylib": {"sha256": "2" * 64, "bytes": 200, "mode": 0o755},
            "Resources/bin/macOS/image-cmyk": {"sha256": "3" * 64, "bytes": 300, "mode": 0o755},
        }
        after = copy.deepcopy(before)
        after["Libraries/lib4d-arm64.dylib"]["sha256"] = "4" * 64
        after["Resources/bin/macOS/image-cmyk"]["sha256"] = "5" * 64
        self.signing = {
            "schema": 1, "commit": self.sha, "version": self.config["version"],
            "finished": True, "result": "partial", "release_ready": False,
            "certificate_sha1": certificate,
            "presign": {"artifact": copy.deepcopy(self.report["artifact"])},
            "artifacts": {
                kind: {"path": "CMYK." + kind, "sha256": publisher.sha256(self.dist / ("CMYK." + kind)),
                       "bytes": (self.dist / ("CMYK." + kind)).stat().st_size}
                for kind in ("zip", "dmg")},
            "payload": {"before": before, "after": after, "code_objects": [
                {"path": path, "before_sha256": before[path]["sha256"], "sha256": after[path]["sha256"],
                 "architectures": arches, "certificate_sha1": certificate, "team_match": True,
                 "timestamp": True, "hardened_runtime": True}
                for path, arches in (("Libraries/lib4d-arm64.dylib", ["arm64"]),
                                     ("Resources/bin/macOS/image-cmyk", ["arm64", "x86_64"]))]},
            "steps": {name: {"status": "passed"} for name in
                      ("payload_signatures", "signed_compiled_harness", "dmg_signature",
                       "notarization", "dmg_stapling", "dmg_assessment", "payload_assessment",
                       "mounted_payload", "cli_smoke")},
        }
        self.signing["steps"]["notarization"]["details"] = {
            "status": "Accepted", "id": "12345678-1234-1234-1234-123456789abc"}
        self.signing["steps"]["signed_compiled_harness"]["details"] = copy.deepcopy(
            self.report["steps"]["compiled_host"]["details"])
        self.signing["steps"]["normal_4d_application"] = {"status": "not_run"}
        self.signing["steps"]["payload_assessment"]["details"] = {
            "method": "strict_online_notarization",
            "cli_spctl": {"status": "passed", "accepted": True, "source": "Notarized Developer ID"},
            "objects": [
                {"path": path, "status": "passed",
                 "command": ["/usr/bin/codesign", "--verify", "--strict", "--all-architectures",
                             "--check-notarization", "-R=notarized", "/staging/" + path]}
                for path in before if path != "CMYK.4DZ"]}
        for name, value in (("load_config", self.config), ("commit", self.sha),
                            ("source_digest", self.digest), ("verify_tag", self.sha)):
            mock = patch.object(publisher, name, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)
        self.save()

    def save(self):
        (self.dist / "CMYK.acceptance.json").write_text(json.dumps(self.report))
        (self.dist / "CMYK.native-evidence.json").write_text(json.dumps(self.native))
        self.signing["presign"]["acceptance_sha256"] = publisher.sha256(self.dist / "CMYK.acceptance.json")
        self.signing["presign"]["native_evidence_sha256"] = publisher.sha256(self.dist / "CMYK.native-evidence.json")
        (self.dist / "CMYK.signing.json").write_text(json.dumps(self.signing))

    def validate(self):
        return publisher.publication_assets(self.root, "v0.1.0-rc.2", self.sha)

    def test_channel_distinguishes_prerelease_from_build_metadata(self):
        for tag in ("v0.1.0-rc.1", "v1.2.3-beta+build-id"):
            self.assertEqual(publisher.release_channel(tag), {"prerelease": True, "latest": False})
        for tag in ("v0.1.0", "v1.2.3+build-with-hyphen"):
            self.assertEqual(publisher.release_channel(tag), {"prerelease": False, "latest": True})
        for tag in ("main", "v01.0.0-rc.1", "v1.0.0-01"):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                publisher.release_channel(tag)

    def test_create_is_atomically_prerelease_never_latest(self):
        with patch.object(publisher.subprocess, "run") as run, \
                patch.object(publisher, "verify_uploaded_assets") as verify_upload, \
                patch.object(publisher, "release_metadata", side_effect=[
                    None, {"id": 123, "draft": True, "prerelease": True},
                    {"draft": False, "prerelease": True}]):
            publisher.publish(self.root, "v0.1.0-rc.2", self.sha)
        args = run.call_args_list[0].args[0]
        self.assertEqual(args[:4], ["gh", "release", "create", "v0.1.0-rc.2"])
        for flag in ("--draft", "--prerelease", "--latest=false", "--verify-tag"):
            self.assertIn(flag, args)
        self.assertTrue(run.call_args.kwargs["check"])
        self.assertEqual(len([arg for arg in args if arg.endswith(".json")]), 3)
        self.assertEqual(verify_upload.call_count, 2)
        self.assertEqual(run.call_args.args[0][:4], ["gh", "api", "--method", "PATCH"])
        self.assertIn("prerelease=true", run.call_args.args[0])
        self.assertIn("make_latest=false", run.call_args.args[0])

    def test_stable_release_is_created_stable_and_explicitly_latest_after_verification(self):
        response = subprocess.CompletedProcess([], 0, stdout='{"tag_name":"v0.1.0"}')
        with patch.object(publisher, "publication_assets", return_value=[]), \
                patch.object(publisher, "verify_uploaded_assets") as verify_upload, \
                patch.object(publisher, "verify_tag"), \
                patch.object(publisher.subprocess, "run", return_value=response) as run, \
                patch.object(publisher, "release_metadata", side_effect=[
                    None, {"id": 124, "draft": True, "prerelease": False},
                    {"draft": False, "prerelease": False}]):
            publisher.publish(self.root, "v0.1.0", self.sha)
        self.assertIn("--prerelease=false", run.call_args_list[0].args[0])
        self.assertIn("make_latest=true", run.call_args_list[-2].args[0])
        self.assertIn("prerelease=false", run.call_args_list[-2].args[0])
        self.assertEqual(verify_upload.call_count, 2)

    def test_existing_release_requires_same_source_channel_and_asset_hashes(self):
        with patch.object(publisher, "release_metadata", return_value={
                "draft": True, "prerelease": True, "body": "unrelated existing draft"}), \
                patch.object(publisher.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "exact source"):
                publisher.publish(self.root, "v0.1.0-rc.2", self.sha)
        run.assert_not_called()

    def test_matching_published_retry_verifies_bytes_and_remote_tag_without_mutation(self):
        assets = self.validate()
        marker = "\n\nVerified release source: " + self.sha + "\n" + "\n".join(
            path.name + " SHA256: " + publisher.sha256(path) for path in assets)
        existing = {"draft": False, "prerelease": True, "body": "notes" + marker}
        with patch.object(publisher, "release_metadata", return_value=existing), \
                patch.object(publisher, "verify_uploaded_assets") as download, \
                patch.object(publisher.subprocess, "run") as run:
            publisher.publish(self.root, "v0.1.0-rc.2", self.sha)
        download.assert_called_once()
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][:2], ["git", "fetch"])

    def test_partial_owned_draft_uploads_only_missing_identical_candidate_assets(self):
        assets = self.validate()
        marker = "\n\nVerified release source: " + self.sha + "\n" + "\n".join(
            path.name + " SHA256: " + publisher.sha256(path) for path in assets)
        draft = {"id": 125, "draft": True, "prerelease": True, "body": "notes" + marker,
                 "assets": [{"name": path.name} for path in assets[:-1]]}
        with patch.object(publisher, "release_metadata", side_effect=[
                draft, draft, {"draft": False, "prerelease": True}]), \
                patch.object(publisher, "verify_uploaded_assets") as download, \
                patch.object(publisher.subprocess, "run") as run:
            publisher.publish(self.root, "v0.1.0-rc.2", self.sha)
        self.assertEqual(run.call_args_list[0].args[0],
                         ["gh", "release", "upload", "v0.1.0-rc.2", str(assets[-1])])
        self.assertEqual(download.call_args_list[0].args[2], assets[:-1])
        self.assertEqual(download.call_count, 3)
        self.assertFalse(any("--clobber" in call.args[0] for call in run.call_args_list))

    def test_rejects_missing_failed_or_wrong_mode_host_gate(self):
        original = copy.deepcopy(self.report)
        for change in ("failed", "unfinished", "wrong-mode", "wrong-config"):
            self.report = copy.deepcopy(original)
            if change == "failed":
                self.report["steps"]["compile"]["status"] = "not_run"
            elif change == "unfinished":
                self.report["finished"] = False
            elif change == "wrong-mode":
                self.report["steps"]["compiled_host"]["details"]["compiled"] = False
            else:
                self.report["configuration"] = {"repository": "other"}
            self.save()
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.validate()

    def test_rejects_modified_archive_or_stale_commit(self):
        (self.dist / "CMYK.zip").write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "verified bytes"):
            self.validate()
        (self.dist / "CMYK.zip").write_bytes(b"synthetic archive bytes")
        self.report["commit"] = "c" * 40
        self.save()
        with self.assertRaisesRegex(ValueError, "fresh completed"):
            self.validate()

    def test_rejects_missing_or_unexecuted_native_target(self):
        original = copy.deepcopy(self.native)
        for change in ("missing", "unexecuted", "mixed-source"):
            self.native = copy.deepcopy(original)
            if change == "missing":
                del self.native["targets"]["windows-x64"]
            elif change == "unexecuted":
                self.native["targets"]["windows-x64"]["execution"] = "not_run"
            else:
                self.native["targets"]["macos-x64"]["source_sha256"] = "d" * 64
            self.save()
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.validate()

    def test_requires_explicit_accepted_notary_and_every_distribution_gate(self):
        original = copy.deepcopy(self.signing)
        for change in ("notary-rejected", "no-staple", "no-cli-assessment", "no-harness", "no-dmg",
                       "wrong-team", "no-runtime", "changed-project", "wrong-architecture",
                       "normal-app-overclaim"):
            self.signing = copy.deepcopy(original)
            if change == "notary-rejected":
                self.signing["steps"]["notarization"]["details"]["status"] = "Invalid"
            elif change in ("no-staple", "no-cli-assessment", "no-harness"):
                step = {"no-staple": "dmg_stapling", "no-cli-assessment": "payload_assessment",
                        "no-harness": "signed_compiled_harness"}[change]
                self.signing["steps"][step]["status"] = "not_run"
            elif change == "no-dmg":
                self.signing["artifacts"]["dmg"]["sha256"] = "0" * 64
            elif change in ("wrong-team", "no-runtime"):
                field = "team_match" if change == "wrong-team" else "hardened_runtime"
                self.signing["payload"]["code_objects"][0][field] = False
            elif change == "changed-project":
                self.signing["payload"]["after"]["CMYK.4DZ"]["sha256"] = "0" * 64
            elif change == "wrong-architecture":
                self.signing["payload"]["code_objects"][1]["architectures"] = ["arm64"]
            else:
                self.signing["steps"]["normal_4d_application"]["status"] = "passed"
            self.save()
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.validate()

    def test_never_promotes_draft_if_uploaded_bytes_fail(self):
        with patch.object(publisher.subprocess, "run") as run, \
                patch.object(publisher, "release_metadata", return_value=None), \
                patch.object(publisher, "verify_uploaded_assets", side_effect=ValueError("changed")):
            with self.assertRaisesRegex(ValueError, "changed"):
                publisher.publish(self.root, "v0.1.0-rc.2", self.sha)
        self.assertEqual(run.call_count, 1)
        self.assertIn("--draft", run.call_args.args[0])

    def test_preflight_validates_final_assets_without_requiring_or_creating_tag(self):
        with patch.object(publisher, "verify_tag") as verify, \
                patch.object(publisher.subprocess, "run") as run:
            assets = publisher.publication_assets(
                self.root, "v0.1.0-rc.2", self.sha, tag_required=False)
        self.assertEqual(len(assets), 5)
        verify.assert_not_called()
        run.assert_not_called()

    def test_requires_direct_ticket_checks_even_for_known_non_app_probe(self):
        assessment = self.signing["steps"]["payload_assessment"]["details"]
        assessment["cli_spctl"] = {
            "status": "failed", "applicable": False, "classification": "valid_non_app",
            "attempted": True, "exit_code": 3,
            "diagnostic": "the code is valid but does not seem to be an app"}
        self.save()
        self.validate()
        original = copy.deepcopy(assessment)
        for change in ("failed-ticket", "missing-ticket-check", "unknown-rejection", "wrong-exit"):
            self.signing["steps"]["payload_assessment"]["details"] = copy.deepcopy(original)
            current = self.signing["steps"]["payload_assessment"]["details"]
            if change == "failed-ticket":
                current["objects"][0]["status"] = "failed"
            elif change == "missing-ticket-check":
                current["objects"][0]["command"].remove("--check-notarization")
            elif change == "unknown-rejection":
                current["cli_spctl"]["diagnostic"] = "Unnotarized Developer ID"
            else:
                current["cli_spctl"]["exit_code"] = 1
            self.save()
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.validate()

    def test_uploaded_bytes_are_downloaded_and_hashed_before_publication(self):
        assets = [self.dist / "CMYK.zip", self.dist / "CMYK.signing.json"]

        def download(args, **kwargs):
            output = Path(args[args.index("--dir") + 1])
            name = args[args.index("--pattern") + 1]
            (output / name).write_bytes((self.dist / name).read_bytes())

        with patch.object(publisher.subprocess, "run", side_effect=download) as run:
            publisher.verify_uploaded_assets(self.root, "v0.1.0-rc.2", assets)
        self.assertEqual(run.call_count, 2)

        def corrupted(args, **kwargs):
            output = Path(args[args.index("--dir") + 1])
            (output / args[args.index("--pattern") + 1]).write_bytes(b"corrupted")

        with patch.object(publisher.subprocess, "run", side_effect=corrupted):
            with self.assertRaisesRegex(ValueError, "differs"):
                publisher.verify_uploaded_assets(self.root, "v0.1.0-rc.2", assets)

    def test_release_metadata_finds_draft_across_pages_not_published_only_tag_endpoint(self):
        pages = [[{"tag_name": "v0.1.0-rc.1", "draft": False}],
                 [{"tag_name": "v0.1.0-rc.2", "draft": True, "prerelease": True}]]
        result = publisher.subprocess.CompletedProcess([], 0, stdout=json.dumps(pages))
        with patch.object(publisher.subprocess, "run", return_value=result) as run:
            metadata = publisher.release_metadata(self.root, "v0.1.0-rc.2")
        self.assertTrue(metadata["draft"])
        self.assertIn("--paginate", run.call_args.args[0])
        self.assertIn("--slurp", run.call_args.args[0])
        self.assertNotIn("/tags/", " ".join(run.call_args.args[0]))
        for invalid in ([], [[pages[1][0], pages[1][0]]]):
            result.stdout = json.dumps(invalid)
            with patch.object(publisher.subprocess, "run", return_value=result):
                with self.assertRaises(ValueError):
                    publisher.release_metadata(self.root, "v0.1.0-rc.2")

    def test_relative_root_produces_absolute_upload_paths(self):
        assets = publisher.publication_assets(
            self.root.relative_to(ROOT), "v0.1.0-rc.2", self.sha)
        self.assertTrue(all(path.is_absolute() for path in assets))

    def test_writer_requires_native_execution(self):
        manifest = next(iter(self.native["targets"].values()))
        with patch.object(publisher, "verify", return_value={**manifest, "execution": "not_run"}):
            with self.assertRaisesRegex(ValueError, "native execution"):
                publisher.write_native_evidence(self.root)

    def test_writer_records_exactly_three_verified_targets(self):
        with patch.object(publisher, "verify",
                          side_effect=lambda root, target, binary: self.native["targets"][target]):
            output = publisher.write_native_evidence(self.root)
        evidence = json.loads(output.read_text())
        self.assertEqual(evidence["targets"], self.native["targets"])
        self.assertEqual(evidence["commit"], self.sha)


if __name__ == "__main__":
    unittest.main()
