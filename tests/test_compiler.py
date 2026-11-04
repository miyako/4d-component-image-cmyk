import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("compile_component", ROOT / "scripts/compile_component.py")
compiler = importlib.util.module_from_spec(spec)
with patch.object(sys, "path", [str(ROOT / "scripts"), *sys.path]):
    spec.loader.exec_module(compiler)


class CompilerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="compile tests ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        config = json.loads((ROOT / "template.json").read_text())
        config["initialized"] = True
        (self.root / "template.json").write_text(json.dumps(config))
        for name in ("package.json", "LICENSE"):
            shutil.copy2(ROOT / name, self.root / name)
        shutil.copytree(ROOT / "tooling/compiler", self.root / "tooling/compiler",
                        ignore=shutil.ignore_patterns("DerivedData", "userPreferences.*", "Data", "Logs"))
        self.name = config["repository"]
        self.feature_class = config.get("feature_class", "Greeting")
        shutil.copytree(ROOT / "component" / self.name, self.root / "component" / self.name,
                        ignore=shutil.ignore_patterns("DerivedData", "Libraries", "Data", "Logs"))
        self.project = self.root / "component" / self.name / "Project" / (self.name + ".4DProject")
        self.status = self.root / "build/compile-status.json"

    def result(self, args, **kwargs):
        self.status.write_text(json.dumps({"success": True, "project": self.project.as_posix(),
                                         "targets": ["x86_64_generic", "arm64_macOS_lib"]}))
        self.assertNotIn("shell", kwargs)
        self.assertIn("--startup-method=Compile", args)
        self.assertTrue(any(a.startswith("--user-param=") for a in args))
        harness = Path(args[1])
        self.assertTrue(harness.is_relative_to(self.root / "build"))
        self.assertNotEqual(harness, self.root / "tooling/compiler/Project/compiler.4DProject")

    def test_real_result_required_and_stale_status_removed(self):
        self.status.parent.mkdir()
        self.status.write_text('{"success":true}')
        with patch.object(compiler.subprocess, "run"), self.assertRaisesRegex(ValueError, "did not write"):
            compiler.compile_component(self.root, "/missing/tool4d")
        self.assertFalse(self.status.exists())

    def test_failed_compilation_not_success_shaped(self):
        def failure(*args, **kwargs):
            self.status.write_text('{"success":false,"errors":[{"message":"bad class"}]}')
        with patch.object(compiler.subprocess, "run", side_effect=failure), self.assertRaisesRegex(ValueError, "bad class"):
            compiler.compile_component(self.root, "/fake/tool4d")

    def test_failed_process_propagates(self):
        with patch.object(compiler.subprocess, "run", side_effect=subprocess.CalledProcessError(1, ["tool4d"])):
            with self.assertRaises(subprocess.CalledProcessError):
                compiler.compile_component(self.root, "/fake/tool4d")

    def test_success_binds_input_digest_and_identity(self):
        with patch.object(compiler.subprocess, "run", side_effect=self.result):
            status = compiler.compile_component(self.root, "/fake/tool4d")
        self.assertEqual(status["input_sha256"], compiler.input_digest(self.root))
        path = self.project.parent / "Sources/Classes" / (self.feature_class + ".4dm")
        path.write_text(path.read_text() + "\n")
        self.assertNotEqual(status["input_sha256"], compiler.input_digest(self.root))

    def test_changed_sources_during_compile_rejected(self):
        def changed(*args, **kwargs):
            self.result(*args, **kwargs)
            path = self.project.parent / "Sources/Classes" / (self.feature_class + ".4dm")
            path.write_text(path.read_text() + "\n")
        with patch.object(compiler.subprocess, "run", side_effect=changed):
            with self.assertRaisesRegex(ValueError, "changed during compilation"):
                compiler.compile_component(self.root, "/fake/tool4d")

    def test_compiler_runtime_outputs_are_isolated_from_versioned_harness(self):
        source = self.root / "tooling/compiler/Project"
        original_settings = (source / "Sources/settings.4DSettings").read_bytes()

        def runtime_outputs(args, **kwargs):
            self.result(args, **kwargs)
            project = Path(args[1]).parent
            (project / "DerivedData").mkdir()
            (project / "DerivedData/cache").write_bytes(b"runtime cache")
            (project / "Sources/catalog.4DCatalog").write_text("<base/>")
            (project / "Sources/settings.4DSettings").write_text("runtime preferences")

        with patch.object(compiler.subprocess, "run", side_effect=runtime_outputs):
            compiler.compile_component(self.root, "/fake/tool4d")
        self.assertFalse((source / "DerivedData").exists())
        self.assertFalse((source / "Sources/catalog.4DCatalog").exists())
        self.assertEqual((source / "Sources/settings.4DSettings").read_bytes(), original_settings)

    @unittest.skipUnless(__import__("os").name == "posix", "symlink requires POSIX")
    def test_compiler_harness_symlink_rejected_before_launch(self):
        method = self.root / "tooling/compiler/Project/Sources/Methods/Compile.4dm"
        outside = self.root / "outside.4dm"
        outside.write_text("sentinel")
        method.unlink()
        method.symlink_to(outside)
        with patch.object(compiler.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "symlinks"):
                compiler.compile_component(self.root, "/fake/tool4d")
            run.assert_not_called()
        self.assertEqual(outside.read_text(), "sentinel")

    def test_wrong_target_and_project_rejected(self):
        def wrong(*args, **kwargs):
            self.status.write_text('{"success":true,"project":"wrong","targets":["x86_64_generic"]}')
        with patch.object(compiler.subprocess, "run", side_effect=wrong):
            with self.assertRaisesRegex(ValueError, "required project and targets"):
                compiler.compile_component(self.root, "/fake/tool4d")

    @unittest.skipUnless(__import__("os").name == "posix", "symlink requires POSIX")
    def test_cleanup_does_not_follow_derived_data_symlink(self):
        outside = self.root / "sentinel-directory"
        outside.mkdir()
        sentinel = outside / "keep"
        sentinel.write_text("not compiler output")
        (self.project.parent / "DerivedData").symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "symlinks"):
            compiler.compile_component(self.root, "/fake/tool4d")
        self.assertEqual(sentinel.read_text(), "not compiler output")
