"""Version computation and file synchronization for the Publish workflow."""
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import bump_release as bump
import ci_config


class NextVersionTests(unittest.TestCase):
    def test_latest_stable_tag_accepts_legacy_v_and_ignores_prereleases(self):
        tags = ["v0.1.0-rc.1", "v0.1.0-rc.2", "v0.1.0", "v0.1.1", "0.2.0-rc.1", "not-a-version", "v01.0.0"]
        self.assertEqual(bump.latest_tag_version(tags), (0, 1, 1))
        self.assertEqual(bump.latest_tag_version(tags + ["0.1.10"]), (0, 1, 10))
        self.assertIsNone(bump.latest_tag_version(["v0.1.0-rc.1", "release"]))

    def test_modes_bump_from_latest_tag(self):
        tags = ["v0.1.0", "v0.1.1", "v0.1.0-rc.2"]
        for mode, expected in (("patch", "0.1.2"), ("minor", "0.2.0"), ("major", "1.0.0")):
            with self.subTest(mode=mode):
                self.assertEqual(bump.next_version(ROOT, mode, tags), expected)
        with self.assertRaises(ValueError):
            bump.next_version(ROOT, "promote", tags)

    def test_without_stable_tags_bumps_configured_version(self):
        version = ci_config.load_config(ROOT)["version"]
        major, minor, patch = (int(part) for part in version.split("-")[0].split("+")[0].split("."))
        self.assertEqual(bump.next_version(ROOT, "patch", ["v0.0.1-rc.1"]), f"{major}.{minor}.{patch + 1}")

    def test_existing_tags_take_precedence_over_configured_version(self):
        self.assertEqual(bump.next_version(ROOT, "patch", ["7.3.9"]), "7.3.10")


class ApplyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix=".bump-test-", dir=ROOT)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        shutil.copytree(ROOT / "component", self.root / "component", symlinks=True,
                        ignore=shutil.ignore_patterns("DerivedData", "Libraries", "bin"))
        for name in ("package.json", "template.json", bump.HOST_CONTRACT):
            destination = self.root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, destination)
        self.previous = ci_config.load_config(self.root)["version"]

    def test_apply_synchronizes_every_version_surface(self):
        changed = bump.apply(self.root, "9.8.7")
        self.assertEqual(changed, sorted(bump.VERSION_FILES))
        self.assertEqual(ci_config.load_config(self.root, tag="9.8.7")["version"], "9.8.7")
        contract = (self.root / bump.HOST_CONTRACT).read_text(encoding="utf-8")
        self.assertNotIn('\\"version\\":\\"' + self.previous + '\\"', contract)
        self.assertEqual(contract.count('\\"version\\":\\"9.8.7\\"'), 2)
        self.assertIn('"version": "9.8.7"', contract)
        json.loads(contract)
        self.assertIn('{protocol:1,version:"9.8.7"}', (self.root / bump.CLASS_DOC).read_text(encoding="utf-8"))

    def test_apply_preserves_unrelated_formatting(self):
        before = (self.root / "package.json").read_text(encoding="utf-8")
        bump.apply(self.root, "9.8.7")
        after = (self.root / "package.json").read_text(encoding="utf-8")
        self.assertEqual(after, before.replace('"version": "' + self.previous + '"', '"version": "9.8.7"'))

    def test_apply_current_version_is_noop_and_rejects_invalid_versions(self):
        self.assertEqual(bump.apply(self.root, self.previous), [])
        for version in ("v1.0.0", "1.0.0-rc.1", "1.0"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                bump.apply(self.root, version)


if __name__ == "__main__":
    unittest.main()
