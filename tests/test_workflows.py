"""Static checks for the CI and single Publish workflow."""
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"
sys.path.insert(0, str(ROOT / "scripts"))
import ci_config


SECRETS = ("APPLE_DEVELOPER_ID_CERTIFICATE", "APPLE_DEVELOPER_ID_CERTIFICATE_PASSWORD",
           "KEYCHAIN_PASSWORD", "NOTARYTOOL_APPLE_ID", "NOTARYTOOL_PASSWORD", "NOTARYTOOL_TEAM_ID")


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.ci = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
        self.publish = (WORKFLOWS / "publish.yml").read_text(encoding="utf-8")

    def test_only_ci_and_publish_exist(self):
        self.assertEqual(sorted(path.name for path in WORKFLOWS.iterdir()), ["ci.yml", "publish.yml"])
        self.assertTrue(self.ci.startswith("name: CI\n"))
        self.assertTrue(self.publish.startswith("name: Publish\n"))

    def test_publish_is_manual_with_a_single_mode_choice(self):
        trigger = self.publish.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
        self.assertIn("workflow_dispatch:", trigger)
        self.assertNotIn("push:", trigger)
        self.assertNotIn("tags", trigger)
        self.assertEqual(trigger.count("type:"), 1)
        self.assertIn("type: choice", trigger)
        for option in ("- patch", "- minor", "- major"):
            self.assertIn(option, trigger)
        for removed in ("version_tag", "dry_run", "preflight", "promote", "candidate", "validation_only"):
            self.assertNotIn(removed, self.publish + self.ci)

    def test_publish_order_bump_build_sign_release(self):
        order = ["bump_release.py next", "bump_release.py apply", "git push origin HEAD:main",
                 "uses: ./.github/workflows/ci.yml", "scripts/accept_local.py",
                 "publish_release.py --write-native-evidence", "scripts/sign_release.py --root . --tool4d",
                 "publish_release.py --validate-signed", 'publish_release.py --version "$VERSION"']
        positions = [self.publish.index(item) for item in order]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("if: github.ref != 'refs/heads/main'", self.publish)
        self.assertIn("cancel-in-progress: false", self.publish)
        self.assertNotIn("continue-on-error", self.publish + self.ci)

    def test_signing_secrets_only_in_publish_build(self):
        for secret in SECRETS:
            self.assertEqual(self.publish.count("${{ secrets." + secret + " }}"), 1)
            self.assertNotIn(secret, self.ci)
        self.assertNotIn("CODESIGN_APPLE_ID", self.publish)
        self.assertIn("python scripts/sign_release.py --root . --cleanup", self.publish)

    def test_ci_builds_exact_publish_commit_and_skips_duplicate_4d_acceptance(self):
        self.assertEqual(self.ci.count("ref: ${{ inputs.ref || github.sha }}"), 3)
        self.assertEqual(self.ci.count("vars.FOURD_ENABLED == 'true' && !inputs.ref"), 2)
        self.assertIn("branches: ['**']", self.ci)
        self.assertIn("ref: ${{ needs.version.outputs.sha }}", self.publish)

    def test_release_tag_validation_accepts_bare_and_legacy_tags(self):
        config = ci_config.load_config(ROOT)
        self.assertEqual(ci_config.load_config(ROOT, tag=config["version"]), config)
        self.assertEqual(ci_config.load_config(ROOT, tag="v" + config["version"]), config)
        with self.assertRaisesRegex(ValueError, "template.json version"):
            ci_config.load_config(ROOT, tag="999999.0.0")


if __name__ == "__main__":
    unittest.main()
