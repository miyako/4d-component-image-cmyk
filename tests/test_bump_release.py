"""Real Git candidate/bundle/finalization fixtures; no remote service or tools."""
import json
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
import bump_release as bump
import ci_config


class SemVerTests(unittest.TestCase):
    def test_strict_parser_and_channel(self):
        self.assertEqual(bump.parse_version("12.34.56-rc.2+build-04"),
                         {"major": 12, "minor": 34, "patch": 56, "prerelease": "rc.2", "build": "build-04"})
        self.assertEqual(bump.parse_version("0.0.0"),
                         {"major": 0, "minor": 0, "patch": 0, "prerelease": "", "build": ""})
        for tag in ("v1.2.3", "v1.2.3+build-hyphen", "v1.2.3+001"):
            self.assertEqual(bump.release_channel(tag), {"prerelease": False, "latest": True})
        self.assertEqual(bump.release_channel("v1.2.3-rc.2+build-hyphen"), {"prerelease": True, "latest": False})
        for version in (None, 1, "", "1.2", "01.2.3", "1.02.3", "1.2.03", "1.2.3-01", "1.2.3-a.00",
                        "1.2.3-", "1.2.3+", "1.2.3-a..b", "1.2.3+a..b", "1.2.3\n", "١.2.3",
                        "v1.2.3", "1.2.3-α", "1.2.3+meta_underscore"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                bump.parse_version(version)
        for tag in ("main", "refs/tags/v1.2.3", "v1.2.3-01", "v1.2.3\n"):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                bump.release_channel(tag)

    def test_numeric_bumps_and_promote(self):
        for action, expected in (("major", "2.0.0"), ("minor", "1.3.0"), ("patch", "1.2.4"), ("promote", "1.2.3")):
            result = bump.planned("1.2.3-rc.2+build-4", action)
            self.assertEqual(result["version"], expected)
            self.assertFalse(result["prerelease"])
            self.assertTrue(result["latest"])
        for version in ("1.2.3", "1.2.3+build-hyphen"):
            with self.assertRaisesRegex(ValueError, "already stable"):
                bump.planned(version, "promote")


class GitFixture(unittest.TestCase):
    def setUp(self):
        self.area = ROOT / (".bump-release-test-" + uuid.uuid4().hex)
        self.area.mkdir()
        self.addCleanup(shutil.rmtree, self.area)
        self.root = self.area / "source"
        self.root.mkdir()
        self.remote = self.area / "remote.git"
        self.git("init", "--bare", "-q", "--initial-branch=main", str(self.remote), cwd=self.area)
        self.git("init", "-q", "--initial-branch=main")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "core.autocrlf", "false")
        self.version = "0.1.0-rc.2"
        self.write_inputs(self.version)
        self.git("add", ".")
        dates = os.environ.copy()
        dates.update(GIT_AUTHOR_DATE="@1700000000 +0000", GIT_COMMITTER_DATE="@1700000000 +0000")
        self.git("commit", "-qm", "base", env=dates)
        self.base = self.git("rev-parse", "HEAD")
        self.configure_origin(self.root)
        self.git("push", "-q", "origin", "main")
        self.directory = self.root / "build/release-candidate"

    def git(self, *args, cwd=None, env=None, data=None, check=True):
        prefix = ["--git-dir", str(self.remote)] if cwd == self.remote else []
        result = subprocess.run(["git", *prefix, *args], cwd=cwd or self.root, env=env, input=data,
                                capture_output=True, check=False)
        if check:
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        return result.stdout.decode("utf-8").strip()

    def file(self, name, content, root=None):
        path = (root or self.root) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode())
        return path

    def write_inputs(self, version):
        config = {"schema": 1, "initialized": True, "repository": "CMYK", "namespace": "CMYK",
                  "cli": "image-cmyk", "language": "cpp", "feature": "fixture color conversion",
                  "version": version, "feature_class": "Converter"}
        package = {"name": "CMYK", "version": version, "tool": {"version": "20.7"}}
        stdout = json.dumps({"protocol": 1, "version": version}, separators=(",", ":")) + "\n"
        contract = {"schema": 1, "feature": {"assertions": [
            {"path": "/version/stdout", "equals": stdout},
            {"path": "/version/value", "equals": {"protocol": 1, "version": version}},
            {"path": "/historic/label", "equals": version}]},
            "execution": {"assertions": [
                {"path": "/renamedExecutable", "equals": {"ok": True, "stdout": stdout, "stderr": "", "exitCode": 0}}]}}
        for name, value in (("package.json", package), ("template.json", config), (bump.HOST_CONTRACT, contract)):
            self.file(name, json.dumps(value, indent=2) + "\n")
        self.file("component/CMYK/Project/CMYK.4DProject", "{}\n")
        self.file("component/CMYK/Project/Sources/settings.4DSettings",
                  '<preferences><general component_classStore_name="CMYK"/></preferences>\n')
        self.file("component/CMYK/Project/Sources/catalog.4DCatalog", '<base name="CMYK"/>\n')
        self.file("component/CMYK/Project/Sources/Classes/Converter.4dm", "// fixture\n")
        self.file(bump.CLASS_DOC, '| `version() : Object` | `{protocol:1,version:"' + version + '"}` |\n')
        self.file("README.md", "Historical release 0.1.0-rc.2; do not globally replace.\n")
        self.file(".gitignore", "build/\ndist/\n")
        self.file(".gitattributes", "* text eol=lf\n")

    def configure_origin(self, checkout):
        self.git("config", "remote.origin.url", "https://github.com/miyako/CMYK.git", cwd=checkout)
        self.git("config", "url." + self.remote.as_uri() + ".insteadOf",
                 "https://github.com/miyako/CMYK.git", cwd=checkout)

    def prepare(self, **kwargs):
        return bump.prepare(self.root, kwargs.pop("action", "patch"), self.base, **kwargs)

    def clone(self, name="clone"):
        checkout = self.area / name
        self.git("clone", "-q", str(self.remote), str(checkout), cwd=self.area)
        self.configure_origin(checkout)
        return checkout

    def transfer(self, checkout):
        destination = checkout / "build/release-candidate"
        destination.parent.mkdir(exist_ok=True)
        shutil.copytree(self.directory, destination)
        return destination

    def env(self):
        return {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": bump.REPOSITORY,
                "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main",
                "GITHUB_SHA": self.base,
                "GITHUB_WORKFLOW_REF": "miyako/CMYK/.github/workflows/bump-release.yml@refs/heads/main"}

    def remote_refs(self):
        return self.git("show-ref", cwd=self.remote)


class CandidateTests(GitFixture):
    def test_plan_and_prepare_do_not_change_remote_or_checkout(self):
        remote, head = self.remote_refs(), self.git("rev-parse", "HEAD")
        before = {name: (self.root / name).read_bytes() for name in bump.VERSION_FILES}
        self.assertEqual(bump.plan(self.root, "patch")["version"], "0.1.1")
        self.assertFalse(self.directory.exists())
        result = self.prepare()
        self.assertEqual(self.remote_refs(), remote)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(before, {name: (self.root / name).read_bytes() for name in bump.VERSION_FILES})
        self.assertEqual(self.git("rev-parse", bump.CANDIDATE_REF), result["source_sha"])
        self.assertEqual(self.git("rev-list", "--parents", "-n", "1", result["source_sha"]),
                         result["source_sha"] + " " + self.base)
        self.assertEqual({path.name for path in self.directory.iterdir()}, {"candidate.bundle", "candidate.json"})

    def test_deterministic_commit_across_clean_clones(self):
        first = self.prepare()
        checkout = self.clone()
        second = bump.prepare(checkout, "patch", self.base)
        self.assertEqual(first["source_sha"], second["source_sha"])
        self.assertEqual(first["tree_sha"], second["tree_sha"])
        commit = self.git("cat-file", "commit", first["source_sha"])
        self.assertIn("Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>", commit)
        self.assertIn("1700000001 +0000", commit)

    def test_full_bundle_restores_fresh_clone_and_exact_version_surfaces(self):
        result = self.prepare()
        checkout = self.clone()
        directory = self.transfer(checkout)
        restored = bump.restore(checkout, directory, result["source_sha"], self.base)
        self.assertEqual(restored, result)
        self.assertEqual(self.git("symbolic-ref", "-q", "HEAD", cwd=checkout, check=False), "")
        self.assertEqual(bump.validate_candidate(checkout, directory, result["source_sha"]), result)
        self.assertEqual(ci_config.load_config(checkout, initialized=True)["version"], "0.1.1")
        self.assertEqual(json.loads((checkout / "package.json").read_text())["tool"]["version"], "20.7")
        self.assertIn("Historical release 0.1.0-rc.2", (checkout / "README.md").read_text())
        self.assertIn('version:"0.1.1"', (checkout / bump.CLASS_DOC).read_text())
        contract = json.loads((checkout / bump.HOST_CONTRACT).read_text())
        self.assertEqual(contract["feature"]["assertions"][1]["equals"]["version"], "0.1.1")
        self.assertEqual(contract["feature"]["assertions"][2]["equals"], self.version)
        self.assertEqual(self.remote_refs(), self.base + " refs/heads/main")
        # A cached checkout can repeat restore without resetting or rewriting.
        self.assertEqual(bump.restore(checkout, directory, result["source_sha"]), result)

    def test_github_output_fields(self):
        output = self.root / "build/outputs"
        output.parent.mkdir()
        with patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}):
            result = self.prepare()
        values = dict(line.split("=", 1) for line in output.read_text().splitlines())
        self.assertEqual(values, {"sha": result["source_sha"], "base_sha": self.base,
                                  "tag": "v0.1.1", "version": "0.1.1", "prerelease": "false"})

    def test_dirty_and_wrong_head_rejected_without_reset_or_push(self):
        self.file("README.md", "dirty user edits\n")
        before = self.remote_refs()
        with self.assertRaisesRegex(ValueError, "dirty sources"):
            self.prepare()
        self.assertEqual((self.root / "README.md").read_text(), "dirty user edits\n")
        self.assertEqual(before, self.remote_refs())
        with self.assertRaisesRegex(ValueError, "dirty sources"):
            bump.restore(self.root, self.directory, self.base)

    def test_wrong_base_and_repeated_prepare_rejected(self):
        with self.assertRaisesRegex(ValueError, "HEAD"):
            bump.prepare(self.root, "patch", "f" * 40)
        self.prepare()
        with self.assertRaisesRegex(ValueError, "restore/reuse"):
            self.prepare()

    def test_untracked_files_rejected_but_ignored_artifacts_survive(self):
        self.file("untracked-user-file", "preserve")
        with self.assertRaisesRegex(ValueError, "clean"):
            self.prepare()
        (self.root / "untracked-user-file").unlink()
        self.file("build/unrelated.log", "preserve")
        self.prepare()
        self.assertEqual((self.root / "build/unrelated.log").read_text(), "preserve")

    def test_metadata_bundle_and_expected_base_tamper(self):
        result = self.prepare()
        path = self.directory / "candidate.json"
        original = path.read_bytes()
        for field, value in (("version", "9.0.0"), ("source_sha", "f" * 40),
                             ("repository", "someone/CMYK"), ("prerelease", True)):
            metadata = json.loads(original)
            metadata[field] = value
            path.write_text(json.dumps(metadata))
            with self.subTest(field=field), self.assertRaises(ValueError):
                bump.restore(self.root, self.directory, result["source_sha"])
            self.assertEqual(self.git("rev-parse", "HEAD"), self.base)
        path.write_bytes(original)
        with self.assertRaisesRegex(ValueError, "base SHA"):
            bump.restore(self.root, self.directory, result["source_sha"], "f" * 40)
        bundle = self.directory / "candidate.bundle"
        bundle.write_bytes(bundle.read_bytes() + b"tamper")
        with self.assertRaisesRegex(ValueError, "checksum"):
            bump.restore(self.root, self.directory, result["source_sha"])

    def test_candidate_worktree_changes_rejected(self):
        result = self.prepare()
        bump.restore(self.root, self.directory, result["source_sha"])
        self.file("README.md", "dirty after build")
        with self.assertRaisesRegex(ValueError, "dirty"):
            bump.validate_candidate(self.root, self.directory, result["source_sha"])

    def test_candidate_rejects_non_version_change_before_checkout(self):
        result = self.prepare()
        index = self.root / "build/forged.index"
        environment = os.environ.copy()
        environment["GIT_INDEX_FILE"] = str(index)
        self.git("read-tree", result["source_sha"], env=environment)
        blob = self.git("hash-object", "-w", "--stdin", data=b"unapproved source change\n")
        self.git("update-index", "--cacheinfo", "100644", blob, "README.md", env=environment)
        tree = self.git("write-tree", env=environment)
        forged = self.git("hash-object", "-t", "commit", "-w", "--stdin",
                          data=bump.expected_commit(tree, self.base, result["version"], result["commit_timestamp"]))
        self.git("update-ref", bump.CANDIDATE_REF, forged)
        bundle = self.directory / "candidate.bundle"
        bundle.unlink()
        self.git("bundle", "create", str(bundle), bump.CANDIDATE_REF)
        result.update(source_sha=forged, tree_sha=tree, bundle_sha256=bump.digest(bundle),
                      changed_paths=sorted([*result["changed_paths"], "README.md"]))
        (self.directory / "candidate.json").write_text(json.dumps(result))
        checkout = self.clone()
        directory = self.transfer(checkout)
        with self.assertRaisesRegex(ValueError, "version-only patch"):
            bump.restore(checkout, directory, forged)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=checkout), self.base)
        self.assertNotIn("unapproved", (checkout / "README.md").read_text())

    def test_thin_bundle_is_rejected_even_when_base_is_available(self):
        result = self.prepare()
        bundle = self.directory / "candidate.bundle"
        bundle.unlink()
        self.git("bundle", "create", str(bundle), bump.CANDIDATE_REF, "^" + self.base)
        result["bundle_sha256"] = bump.digest(bundle)
        (self.directory / "candidate.json").write_text(json.dumps(result))
        with self.assertRaisesRegex(ValueError, "full bundle"):
            bump.restore(self.root, self.directory, result["source_sha"])
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)

    def test_dirty_fresh_clone_is_not_reset_during_restore(self):
        result = self.prepare()
        checkout = self.clone()
        directory = self.transfer(checkout)
        self.file("README.md", "uncommitted local work\n", root=checkout)
        with self.assertRaisesRegex(ValueError, "dirty sources"):
            bump.restore(checkout, directory, result["source_sha"])
        self.assertEqual((checkout / "README.md").read_text(), "uncommitted local work\n")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=checkout), self.base)

    def test_config_package_authority_and_mismatch(self):
        config = json.loads((self.root / "template.json").read_text())
        config["version"] = "9.9.9"
        self.file("template.json", json.dumps(config))
        with self.assertRaisesRegex(ValueError, "must match"):
            bump.plan(self.root, "patch")
        with self.assertRaisesRegex(ValueError, "must match"):
            ci_config.load_config(self.root)

    def test_stable_input_is_not_tied_to_repository_current_prerelease(self):
        self.write_inputs("0.1.1")
        self.git("add", ".")
        self.git("commit", "-qm", "stable fixture")
        self.base = self.git("rev-parse", "HEAD")
        self.assertEqual(bump.plan(self.root, "patch")["version"], "0.1.2")
        result = self.prepare()
        bump.restore(self.root, self.directory, result["source_sha"])
        self.assertEqual(bump.validate_candidate(self.root, self.directory, result["source_sha"])["version"], "0.1.2")

    def test_wrong_origin_and_unsafe_output_paths_rejected(self):
        self.git("config", "remote.origin.url", "https://github.com/miyako/Other.git")
        with self.assertRaisesRegex(ValueError, "exact miyako/CMYK"):
            bump.plan(self.root, "patch")
        self.configure_origin(self.root)
        with self.assertRaisesRegex(ValueError, "root/build"):
            self.prepare(output_dir=self.area / "outside")

    def test_prepare_requires_full_history(self):
        checkout = self.area / "shallow"
        self.git("clone", "-q", "--depth=1", self.remote.as_uri(), str(checkout), cwd=self.area)
        self.configure_origin(checkout)
        with self.assertRaisesRegex(ValueError, "full history"):
            bump.prepare(checkout, "patch", self.base)
        self.assertFalse((checkout / "build/release-candidate").exists())


class ProductionTests(GitFixture):
    def ready(self):
        result = self.prepare()
        bump.restore(self.root, self.directory, result["source_sha"])
        return result

    def test_production_prepare_keeps_main_and_tag_remote_untouched(self):
        before = self.remote_refs()
        with patch.dict(os.environ, self.env()):
            result = self.prepare(production=True)
        self.assertEqual(self.remote_refs(), before)
        self.assertEqual(result["base_sha"], self.base)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)

    def test_production_prepare_refuses_existing_remote_target_tag(self):
        self.git("tag", "v0.1.1", self.base)
        self.git("push", "-q", "origin", "refs/tags/v0.1.1")
        with patch.dict(os.environ, self.env()):
            with self.assertRaisesRegex(ValueError, "target tag"):
                self.prepare(production=True)
        self.assertFalse(self.directory.exists())
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)

    def test_production_event_ref_workflow_and_base_guards(self):
        result = self.ready()
        cases = ({"GITHUB_ACTIONS": "false"}, {"GITHUB_EVENT_NAME": "pull_request"},
                 {"GITHUB_REF": "refs/heads/feature"}, {"GITHUB_SHA": "f" * 40},
                 {"GITHUB_WORKFLOW_REF": "miyako/CMYK/.github/workflows/other.yml@refs/heads/main"})
        for change in cases:
            env = self.env()
            env.update(change)
            with self.subTest(change=change), patch.dict(os.environ, env):
                with self.assertRaises(ValueError):
                    bump.validate_candidate(self.root, self.directory, result["source_sha"], production=True)
        self.assertEqual(self.remote_refs(), self.base + " refs/heads/main")

    def test_finalize_validates_assets_before_atomic_push_and_is_resumable(self):
        result = self.ready()
        calls = []

        def accepted(root, tag, sha, **kwargs):
            self.assertEqual(self.remote_refs(), self.base + " refs/heads/main")
            self.assertEqual((tag, sha, kwargs), ("v0.1.1", result["source_sha"], {"tag_required": False}))
            calls.append("validated")

        with patch.dict(os.environ, self.env()), patch("publish_prerelease.publication_assets", side_effect=accepted):
            finalized = bump.finalize(self.root, self.directory, result["source_sha"])
        self.assertEqual(calls, ["validated"])
        self.assertTrue(finalized["finalized"])
        self.assertFalse(finalized["resumed"])
        self.assertEqual(bump.remote_state(self.root, result["tag"]), (result["source_sha"], result["source_sha"]))
        self.git("tag", "-d", result["tag"])
        with patch.dict(os.environ, self.env()), patch("publish_prerelease.publication_assets") as assets:
            resumed = bump.finalize(self.root, self.directory, result["source_sha"])
        assets.assert_called_once()
        self.assertTrue(resumed["resumed"])
        self.assertEqual(self.git("rev-parse", "refs/tags/" + result["tag"]), result["source_sha"])

    def test_failed_asset_gate_never_creates_tag_or_pushes(self):
        result = self.ready()
        with patch.dict(os.environ, self.env()), \
                patch("publish_prerelease.publication_assets", side_effect=ValueError("notary gate failed")):
            with self.assertRaisesRegex(ValueError, "notary"):
                bump.finalize(self.root, self.directory, result["source_sha"])
        self.assertEqual(self.git("tag", "--list"), "")
        self.assertEqual(self.remote_refs(), self.base + " refs/heads/main")

    def test_main_race_after_assets_fails_without_tag_or_force(self):
        result = self.ready()
        tree = self.git("rev-parse", self.base + "^{tree}")
        other = self.git("commit-tree", tree, "-p", self.base, data=b"concurrent main\n")

        def race(*args, **kwargs):
            self.git("push", "-q", "origin", other + ":refs/heads/main")

        with patch.dict(os.environ, self.env()), patch("publish_prerelease.publication_assets", side_effect=race):
            with self.assertRaisesRegex(ValueError, "main moved"):
                bump.finalize(self.root, self.directory, result["source_sha"])
        self.assertEqual(self.git("tag", "--list"), "")
        self.assertEqual(bump.remote_state(self.root, result["tag"]), (other, None))

    def test_atomic_push_tag_race_never_updates_main(self):
        result = self.ready()
        real_git = bump.git
        pushes = []

        def raced_git(root, *arguments, **kwargs):
            if arguments and arguments[0] == "push":
                pushes.append(arguments)
                self.git("update-ref", "refs/tags/" + result["tag"], self.base, cwd=self.remote)
            return real_git(root, *arguments, **kwargs)

        with patch.dict(os.environ, self.env()), patch("publish_prerelease.publication_assets"), \
                patch.object(bump, "git", side_effect=raced_git):
            with self.assertRaisesRegex(ValueError, "atomic release push rejected"):
                bump.finalize(self.root, self.directory, result["source_sha"])
        self.assertEqual(len(pushes), 1)
        self.assertIn("--atomic", pushes[0])
        self.assertIn("--no-follow-tags", pushes[0])
        self.assertFalse(any(argument.startswith("--force") or argument.startswith("+") for argument in pushes[0]))
        self.assertEqual(bump.remote_state(self.root, result["tag"]), (self.base, self.base))

    def test_partial_or_foreign_finalization_is_not_a_resume(self):
        result = self.ready()
        self.git("push", "-q", "origin", result["source_sha"] + ":refs/heads/main")
        with patch.dict(os.environ, self.env()):
            with self.assertRaisesRegex(ValueError, "origin/main moved"):
                bump.validate_candidate(self.root, self.directory, result["source_sha"], production=True)


if __name__ == "__main__":
    unittest.main()
