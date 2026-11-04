"""Publication guards use synthetic evidence; tests never invoke GitHub."""
import copy
import hashlib
import json
from pathlib import Path
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
        self.config = {"repository": "CMYK", "version": "0.1.0-rc.1", "cli": "image-cmyk"}
        (self.dist / "CMYK.zip").write_bytes(b"synthetic archive bytes")
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
                "success": True, "finished": True, "compiled": compiled}
        self.report["steps"]["compile"]["details"] = {
            "success": True, "errors": [], "targets": ["x86_64_generic", "arm64_macOS_lib"],
            "input_sha256": "c" * 64, "output_sha256": "d" * 64}
        self.native = {"schema": 1, "commit": self.sha, "source_sha256": self.digest,
                       "targets": {target: {"target": target, "commit": self.sha,
                                           "source_sha256": self.digest, "execution": "passed"}
                                   for target in publisher.TARGETS}}
        for name, value in (("load_config", self.config), ("commit", self.sha),
                            ("source_digest", self.digest), ("verify_tag", self.sha)):
            mock = patch.object(publisher, name, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)
        self.save()

    def save(self):
        (self.dist / "CMYK.acceptance.json").write_text(json.dumps(self.report))
        (self.dist / "CMYK.native-evidence.json").write_text(json.dumps(self.native))

    def validate(self):
        return publisher.publication_assets(self.root, "v0.1.0-rc.1", self.sha)

    def test_only_real_semver_prereleases_are_allowed(self):
        for tag in ("v0.1.0-rc.1", "v1.2.3-beta+build-id"):
            publisher.require_prerelease(tag)
        for tag in ("v0.1.0", "v1.2.3+build-with-hyphen", "main", "v01.0.0-rc.1"):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                publisher.require_prerelease(tag)

    def test_create_is_atomically_prerelease_never_latest(self):
        with patch.object(publisher.subprocess, "run") as run:
            publisher.publish(self.root, "v0.1.0-rc.1", self.sha)
        args = run.call_args.args[0]
        self.assertEqual(args[:4], ["gh", "release", "create", "v0.1.0-rc.1"])
        for flag in ("--prerelease", "--latest=false", "--verify-tag"):
            self.assertIn(flag, args)
        self.assertTrue(run.call_args.kwargs["check"])
        self.assertEqual(len([arg for arg in args if arg.endswith(".json")]), 2)

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
        with self.assertRaisesRegex(ValueError, "accepted bytes"):
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
