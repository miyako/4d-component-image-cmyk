"""Release refs use real local Git tags; no workflows or releases are invoked."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import ci_config
import release_ref


class ReleaseRefTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / (".release-ref-test-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.git("init", "-q")
        self.git("config", "user.name", "Release fixture")
        self.git("config", "user.email", "release-fixture@example.invalid")
        self.git("commit", "-q", "--allow-empty", "-m", "first")
        self.sha = self.git("rev-parse", "HEAD")
        self.git("tag", "0.1.0")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, check=True,
                              capture_output=True, text=True, encoding="utf-8").stdout.strip()

    def resolve(self, **args):
        defaults = {"event": "workflow_dispatch", "version_tag": "0.1.0"}
        defaults.update(args)
        return release_ref.resolve(self.root, **defaults)

    def test_manual_branch_resolves_existing_tag_not_selected_branch(self):
        self.git("commit", "-q", "--allow-empty", "-m", "branch advanced")
        metadata = self.resolve(ref_type="branch", ref_name="main",
                                event_sha=self.git("rev-parse", "HEAD"))
        self.assertEqual(metadata, {"tag": "0.1.0", "sha": self.sha})

    def test_push_tag_requires_event_commit(self):
        self.assertEqual(self.resolve(event="push", ref_type="tag", ref_name="0.1.0",
                                      event_sha=self.sha),
                         {"tag": "0.1.0", "sha": self.sha})

    def test_push_branch_and_unsupported_events_fail(self):
        for args in ({"event": "push", "ref_type": "branch"},
                     {"event": "pull_request"}, {"event": "workflow_call"}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.resolve(**args)

    def test_annotated_tag_peels_to_commit(self):
        self.git("tag", "-a", "0.2.0", "-m", "annotated")
        self.assertNotEqual(self.git("rev-parse", "refs/tags/0.2.0"), self.sha)
        self.assertEqual(self.resolve(version_tag="0.2.0")["sha"], self.sha)

    def test_legacy_v_prefixed_existing_tag_still_resolves(self):
        self.git("tag", "v0.0.9")
        self.assertEqual(self.resolve(version_tag="v0.0.9"), {"tag": "v0.0.9", "sha": self.sha})

    def test_semver_prerelease_and_build_metadata(self):
        tag = "1.2.3-rc.1+build-4"
        self.git("tag", tag)
        self.assertEqual(self.resolve(version_tag=tag)["sha"], self.sha)

    def test_malformed_and_arbitrary_refs_rejected_before_git(self):
        for tag in ("", "main", "refs/tags/0.1.0", "01.2.3", "1.2",
                    "1.2.3-01", "1.2.3\nsha=evil", "1.2.3^{commit}",
                    "--help", "1.2.3;echo x", "v١.2.3", "vv1.2.3", "V1.2.3", None):
            with self.subTest(tag=tag), patch.object(release_ref.subprocess, "run") as run:
                with self.assertRaises(ValueError):
                    self.resolve(version_tag=tag)
                run.assert_not_called()

    def test_missing_tag_fails(self):
        with self.assertRaisesRegex(ValueError, "already exist"):
            self.resolve(version_tag="9.9.9")

    def preflight(self, **args):
        self.git("branch", "main")
        self.git("remote", "add", "origin", str(self.root))
        defaults = {"preflight": True, "ref_type": "branch", "ref_name": "main",
                    "event_sha": self.sha, "version_tag": ""}
        defaults.update(args)
        with patch.object(release_ref, "load_config", return_value={"version": "0.1.0-rc.2"}):
            return self.resolve(**defaults)

    def test_preflight_validates_main_without_creating_a_tag(self):
        self.assertEqual(self.preflight(), {"tag": "0.1.0-rc.2", "sha": self.sha})
        self.assertEqual(self.git("tag", "--list"), "0.1.0")

    def test_preflight_rejects_nonproduction_refs_before_commands(self):
        for args in ({"event": "pull_request"}, {"event": "push"}, {"ref_name": "feature"},
                     {"ref_type": "tag"}, {"event_sha": "HEAD"}):
            with self.subTest(args=args), patch.object(release_ref.subprocess, "run") as run:
                with self.assertRaises(ValueError):
                    self.resolve(preflight=True, ref_type=args.get("ref_type", "branch"),
                                 ref_name=args.get("ref_name", "main"),
                                 event=args.get("event", "workflow_dispatch"),
                                 event_sha=args.get("event_sha", self.sha))
                run.assert_not_called()

    def test_preflight_rejects_stale_event_or_wrong_version(self):
        with self.assertRaisesRegex(ValueError, "current origin/main"):
            self.preflight(event_sha="f" * 40)
        with patch.object(release_ref, "load_config", return_value={"version": "0.1.0-rc.2"}):
            with self.assertRaisesRegex(ValueError, "matching version"):
                self.resolve(preflight=True, ref_type="branch", ref_name="main",
                             event_sha=self.sha, version_tag="0.1.0-rc.9")

    def test_tag_pointing_at_blob_fails(self):
        (self.root / "blob").write_text("not a commit", encoding="utf-8")
        blob = self.git("hash-object", "-w", "blob")
        self.git("tag", "0.3.0", blob)
        with self.assertRaisesRegex(ValueError, "point to a commit"):
            self.resolve(version_tag="0.3.0")

    def test_verify_expected_commit_and_invalid_sha(self):
        self.assertEqual(release_ref.verify(self.root, "0.1.0", self.sha), self.sha)
        for sha in ("", self.sha[:7], "HEAD", "A" * 40, "f" * 41, None):
            with self.subTest(sha=sha), self.assertRaisesRegex(ValueError, "full normalized"):
                release_ref.verify(self.root, "0.1.0", sha)

    def test_moved_tag_fails_push_and_publish(self):
        self.git("commit", "-q", "--allow-empty", "-m", "second")
        self.git("tag", "-f", "0.1.0")
        with self.assertRaisesRegex(ValueError, "moved"):
            release_ref.verify(self.root, "0.1.0", self.sha)
        with self.assertRaisesRegex(ValueError, "moved"):
            self.resolve(event="push", ref_type="tag", ref_name="0.1.0",
                         event_sha=self.sha)

    def test_deleted_tag_fails_publish(self):
        self.git("tag", "-d", "0.1.0")
        with self.assertRaisesRegex(ValueError, "already exist"):
            release_ref.verify(self.root, "0.1.0", self.sha)

    def test_git_uses_literal_tag_ref_argument_and_rejects_bad_output(self):
        result = subprocess.CompletedProcess([], 0, stdout="not-a-sha\n", stderr="")
        with patch.object(release_ref.subprocess, "run", return_value=result) as run:
            with self.assertRaisesRegex(ValueError, "invalid commit SHA"):
                release_ref.tag_commit(self.root, "0.1.0")
            self.assertEqual(run.call_args.args[0],
                             ["git", "rev-parse", "--verify", "refs/tags/0.1.0^{commit}"])
            self.assertNotIn("shell", run.call_args.kwargs)

    def test_cli_outputs_only_validated_single_line_metadata(self):
        output = self.root / "github-output"
        output.write_text("previous=step\n", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/release_ref.py"), "--root", str(self.root),
             "resolve", "--event", "workflow_dispatch", "--version-tag", "0.1.0"],
            env={**os.environ, "GITHUB_OUTPUT": str(output)}, capture_output=True,
            text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = f"tag=0.1.0\nsha={self.sha}\n"
        self.assertEqual(result.stdout, expected)
        self.assertEqual(result.stderr, "")
        self.assertEqual(output.read_text(encoding="utf-8"), "previous=step\n" + expected)

    def test_cli_failure_leaves_metadata_unchanged(self):
        output = self.root / "github-output"
        output.write_text("previous=step\n", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/release_ref.py"), "--root", str(self.root),
             "resolve", "--event", "workflow_dispatch", "--version-tag", "9.9.9"],
            env={**os.environ, "GITHUB_OUTPUT": str(output)}, capture_output=True,
            text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("already exist", result.stderr)
        self.assertEqual(output.read_text(encoding="utf-8"), "previous=step\n")

    def test_existing_configuration_validator_checks_version(self):
        config = ci_config.load_config(ROOT)
        self.assertEqual(ci_config.load_config(ROOT, tag=config["version"]), config)
        # Legacy v-prefixed tags (v0.1.1 and earlier) remain accepted for existing releases.
        self.assertEqual(ci_config.load_config(ROOT, tag="v" + config["version"]), config)
        with self.assertRaisesRegex(ValueError, "template.json version"):
            ci_config.load_config(ROOT, tag="999999.0.0")

    def test_workflows_pin_every_post_gate_checkout_and_package_tag(self):
        ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertIn("tags: ['[0-9]+.[0-9]+.[0-9]+*']", release)
        self.assertNotIn("'v*'", release)
        self.assertEqual(ci.count("uses: actions/checkout@v4"), 3)
        self.assertEqual(ci.count("ref: ${{ inputs.source_bundle && github.sha || inputs.source_sha || github.sha }}"), 3)
        self.assertEqual(ci.count("scripts/bump_release.py restore"), 3)
        self.assertEqual(ci.count(
            "name: Restore exact unpushed version candidate\n"
            "        if: inputs.source_bundle != ''\n        shell: bash\n"), 3)
        self.assertEqual(ci.count("&& !inputs.release_call"), 2)
        self.assertEqual(ci.count("github.event_name == 'workflow_dispatch' && inputs.accept_4d"), 2)
        self.assertIn("python scripts/accept_local.py --root .", ci)
        self.assertIn("set -o pipefail", ci)
        self.assertIn("TOOL4D_URL: ${{ !inputs.accept_4d && vars.TOOL4D_URL || '' }}", ci)
        self.assertIn("TOOL4D_SHA256: ${{ !inputs.accept_4d && vars.TOOL4D_SHA256 || '' }}", ci)
        self.assertIn("if: always() && steps.acceptance.outcome", ci)
        self.assertIn("name: four-d-acceptance", ci)
        self.assertNotIn("continue-on-error", ci)
        self.assertIn("workflow_dispatch:", ci)
        self.assertIn("workflow_dispatch:", release)
        self.assertIn("fetch-depth: 0", release)
        self.assertIn("source_sha: ${{ needs.gate.outputs.source_sha }}", release)
        build = (ROOT / ".github/workflows/release-build.yml").read_text()
        self.assertIn("uses: ./.github/workflows/release-build.yml", release)
        self.assertIn("release_call: true", build)
        self.assertEqual(release.count("ref: ${{ needs.gate.outputs.source_sha }}"), 1)
        self.assertEqual((release + build).count('validate --release --tag "$RELEASE_TAG"'), 2)
        self.assertIn('scripts/accept_local.py --root . --repository "$GITHUB_REPOSITORY"', build)
        self.assertIn("set -o pipefail", build)
        self.assertIn("scripts/publish_prerelease.py --write-native-evidence", build)
        self.assertIn("dist/*.acceptance.json", build)
        self.assertIn("dist/*.native-evidence.json", build)
        self.assertLess(release.index("release_ref.py verify"),
                        release.index('publish_prerelease.py --tag "$RELEASE_TAG"'))
        self.assertIn('git fetch --force --no-tags origin "+refs/tags/$RELEASE_TAG:refs/tags/$RELEASE_TAG"', release)
        publisher = (ROOT / "scripts/publish_prerelease.py").read_text(encoding="utf-8")
        for flag in ("--verify-tag", "--prerelease", "--latest=false"):
            self.assertIn(flag, publisher)

    def test_signing_is_production_only_and_preflight_never_publishes(self):
        release = (ROOT / ".github/workflows/release.yml").read_text()
        build = (ROOT / ".github/workflows/release-build.yml").read_text()
        ci = (ROOT / ".github/workflows/ci.yml").read_text()
        self.assertIn("if: github.event_name == 'push' || github.event_name == 'workflow_dispatch'", release)
        self.assertIn("if: github.event_name != 'workflow_dispatch' || !inputs.preflight", release)
        self.assertIn("scripts/sign_release.py --root .", build)
        self.assertIn("dist/*.dmg", build)
        self.assertIn("dist/*.signing.json", build)
        self.assertNotIn("continue-on-error", release + build)
        for secret in ("APPLE_DEVELOPER_ID_CERTIFICATE", "APPLE_DEVELOPER_ID_CERTIFICATE_PASSWORD",
                       "KEYCHAIN_PASSWORD", "NOTARYTOOL_APPLE_ID", "NOTARYTOOL_PASSWORD",
                       "NOTARYTOOL_TEAM_ID"):
            self.assertEqual(build.count("${{ secrets." + secret + " }}"), 1)
            self.assertNotIn(secret, ci)
        self.assertNotIn("CODESIGN_APPLE_ID", release)
        self.assertIn("TOOL4D_URL: ''", build)
        self.assertIn("TOOL4D_SHA256: ''", build)

    def test_signing_checkout_retains_tag_refs_for_exact_push_verification(self):
        release = (ROOT / ".github/workflows/release-build.yml").read_text()
        compilation = release.split("\n  compile:\n", 1)[1]
        self.assertIn("ref: ${{ inputs.source_bundle && github.sha || inputs.source_sha }}\n          fetch-depth: 0", compilation)

    def test_bump_pr_validation_cannot_sign_push_or_publish(self):
        workflow = (ROOT / ".github/workflows/bump-release.yml").read_text()
        build = (ROOT / ".github/workflows/release-build.yml").read_text()
        validation = workflow.split("\n  validate:\n", 1)[1].split("\n  production:\n", 1)[0]
        self.assertIn("dry_run: true", validation)
        self.assertNotIn("secrets:", validation)
        self.assertIn("github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main'", workflow)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertIn("scripts/bump_release.py finalize", workflow)
        self.assertIn("presign_assets", workflow)
        self.assertIn("production release builds cannot run on PR events", build)
        self.assertEqual(build.count("if: '!inputs.dry_run'"), 2)
        self.assertIn("if: always() && !inputs.dry_run", build)
        self.assertLess(build.index("Require candidate invariants after native and compiler outputs"),
                        build.index("Sign, notarize and verify the exact production distribution"))
        self.assertIn('print("Post-build source status:', build)


if __name__ == "__main__":
    unittest.main()
