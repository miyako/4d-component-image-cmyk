import hashlib
import io
import json
import shutil
from pathlib import Path, PureWindowsPath
import sys
import tarfile
import urllib.error
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import build_dependencies
from build_cli import audit_native_dependencies
from build_provenance import source_digest
from package_component import validate_dependency_notices


class DependencyTests(unittest.TestCase):
    def cached_archive(self, root, name="source/file", kind=tarfile.REGTYPE):
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w:gz") as archive:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.linkname = "../outside" if kind != tarfile.REGTYPE else ""
            content = b"immutable upstream source"
            member.size = len(content) if kind == tarfile.REGTYPE else 0
            archive.addfile(member, io.BytesIO(content) if member.size else None)
        (root / "cli").mkdir()
        cache = root / "build/dependency-sources"
        cache.mkdir(parents=True)
        digest = hashlib.sha256(data.getvalue()).hexdigest()
        (cache / ("example-" + digest + ".archive")).write_bytes(data.getvalue())
        (root / "cli/dependencies.json").write_text(json.dumps({"dependencies": [
            {"name": "example", "sha256": digest}]}))

    def test_pins_and_distributed_notices(self):
        lock = json.loads((ROOT / "cli/dependencies.json").read_text())
        self.assertEqual(len(lock["dependencies"]), 6)
        directory = ROOT / "component/4d-component-image-cmyk/Resources/licenses"
        manifest = json.loads((directory / "provenance.json").read_text())
        pins = {entry["name"]: entry for entry in lock["dependencies"]}
        for notice in manifest["notices"]:
            self.assertEqual(notice["source_sha256"], pins[notice["dependency"]]["sha256"])
            self.assertEqual(hashlib.sha256((directory / notice["path"]).read_bytes()).hexdigest(),
                             notice["sha256"])
        self.assertEqual({entry["dependency"] for entry in manifest["notices"]}, set(pins))

    def test_inventory_uses_portable_paths_and_detects_changes(self):
        self.assertEqual(PureWindowsPath("lib", "jpeg.lib").as_posix(), "lib/jpeg.lib")
        with tempfile.TemporaryDirectory(prefix=".dependency-test-", dir=ROOT) as temporary:
            root = Path(temporary)
            library = root / "lib/jpeg.lib"
            library.parent.mkdir()
            library.write_bytes(b"first")
            original = build_dependencies.static_inventory(root)
            self.assertEqual(set(original), {"lib/jpeg.lib"})
            library.write_bytes(b"changed")
            self.assertNotEqual(original, build_dependencies.static_inventory(root))

    def test_cmake_paths_do_not_emit_windows_escape_sequences(self):
        windows = PureWindowsPath("D:/a/repository/dependency sources/Little-CMS")
        for name in ("LCMS_SOURCE", "JSON_SOURCE", "IMAGE_DEPENDENCY_PREFIX",
                     "CMAKE_INSTALL_PREFIX", "CMAKE_PREFIX_PATH", "ZLIB_LIBRARY"):
            option = build_dependencies.cmake_path_option(name, windows)
            self.assertEqual(option, "-D" + name + "=D:/a/repository/dependency sources/Little-CMS")
            self.assertNotIn("\\", option)

    def test_provenance_binds_dependency_build_recipe(self):
        with tempfile.TemporaryDirectory(prefix=".dependency-test-", dir=ROOT) as temporary:
            root = Path(temporary)
            (root / "cli").mkdir()
            (root / "scripts").mkdir()
            for name in ("template.json", "package.json"):
                (root / name).write_text("{}")
            (root / "cli/dependencies.json").write_text("{}")
            recipe = root / "scripts/build_dependencies.py"
            recipe.write_text("first")
            original = source_digest(root)
            recipe.write_text("second")
            self.assertNotEqual(original, source_digest(root))

    def test_package_rejects_modified_notice(self):
        with tempfile.TemporaryDirectory(prefix=".dependency-test-", dir=ROOT) as temporary:
            root = Path(temporary)
            (root / "cli").mkdir()
            shutil.copyfile(ROOT / "cli/dependencies.json", root / "cli/dependencies.json")
            component = root / "component"
            notices = component / "Resources/licenses"
            shutil.copytree(ROOT / "component/4d-component-image-cmyk/Resources/licenses", notices)
            validate_dependency_notices(root, component)
            manifest = json.loads((notices / "provenance.json").read_text())
            (notices / manifest["notices"][0]["path"]).write_text("modified")
            with self.assertRaisesRegex(ValueError, "modified dependency notice"):
                validate_dependency_notices(root, component)

    def test_source_cache_rejects_mutation(self):
        with tempfile.TemporaryDirectory(prefix=".dependency-test-", dir=ROOT) as temporary:
            root = Path(temporary)
            self.cached_archive(root)
            source = build_dependencies.sources(root)["example"]
            self.assertEqual((source / "file").read_bytes(), b"immutable upstream source")
            (source / "file").write_bytes(b"modified")
            with self.assertRaisesRegex(ValueError, "modified extracted dependency"):
                build_dependencies.sources(root)

    def test_unsafe_upstream_archives_are_rejected(self):
        for name, kind in (
            ("../escape", tarfile.REGTYPE), ("/absolute", tarfile.REGTYPE),
            ("C:/escape", tarfile.REGTYPE), ("source\\escape", tarfile.REGTYPE),
            ("source/link", tarfile.SYMTYPE), ("source/link", tarfile.LNKTYPE),
        ):
            with self.subTest(name=name, kind=kind), tempfile.TemporaryDirectory(
                    prefix=".dependency-test-", dir=ROOT) as temporary:
                root = Path(temporary)
                self.cached_archive(root, name, kind)
                with self.assertRaisesRegex(ValueError, "unsafe dependency archive"):
                    build_dependencies.sources(root)

    def test_archive_checksum_mismatch_is_not_extracted(self):
        with tempfile.TemporaryDirectory(prefix=".dependency-test-", dir=ROOT) as temporary:
            root = Path(temporary)
            self.cached_archive(root)
            next((root / "build/dependency-sources").glob("*.archive")).write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                build_dependencies.sources(root)

    def test_native_macos_dependency_closure_rejects_build_machine_rpaths(self):
        for library, accepted in (("/usr/lib/libc++.1.dylib", True),
                                  ("@rpath/libz.1.dylib", False),
                                  ("/opt/homebrew/lib/libpng16.dylib", False)):
            output = f"image-cmyk:\n\t{library} (compatibility version 1.0.0, current version 1.0.0)\n"
            with self.subTest(library=library), patch(
                    "build_cli.subprocess.run", return_value=SimpleNamespace(stdout=output)):
                if accepted:
                    self.assertEqual(audit_native_dependencies("image-cmyk", "macos-arm64"), [library])
                else:
                    with self.assertRaisesRegex(ValueError, "non-system macOS"):
                        audit_native_dependencies("image-cmyk", "macos-arm64")

    def test_windows_dependency_closure_requires_static_crt(self):
        with tempfile.TemporaryDirectory(prefix=".dependency-test-", dir=ROOT) as temporary:
            root = Path(temporary)
            (root / "dumpbin.exe").touch()
            for library, accepted in (("KERNEL32.dll", True), ("VCRUNTIME140.dll", False),
                                      ("libpng16.dll", False)):
                with self.subTest(library=library), patch(
                        "build_cli.subprocess.run",
                        return_value=SimpleNamespace(stdout="    " + library + "\n")):
                    if accepted:
                        self.assertEqual(audit_native_dependencies(
                            "image-cmyk.exe", "windows-x64", root / "cl.exe"), [library])
                    else:
                        with self.assertRaisesRegex(ValueError, "unreviewed Windows DLL"):
                            audit_native_dependencies("image-cmyk.exe", "windows-x64", root / "cl.exe")

    def test_transient_download_retries_same_pin_only(self):
        dependency = {"name": "example", "url": "https://example.test/fixed"}
        response = io.BytesIO(b"archive")
        response.geturl = lambda: dependency["url"]
        transient = urllib.error.HTTPError(dependency["url"], 502, "Bad Gateway", {}, None)
        with tempfile.TemporaryDirectory(prefix=".dependency-test-", dir=ROOT) as temporary, \
                patch("build_dependencies.urllib.request.urlopen",
                      side_effect=[transient, response]) as request, \
                patch("build_dependencies.time.sleep"):
            output = Path(temporary) / "download"
            build_dependencies.download_archive(dependency, output)
            self.assertEqual(output.read_bytes(), b"archive")
            self.assertEqual([call.args for call in request.call_args_list],
                             [(dependency["url"],), (dependency["url"],)])

    def test_permanent_download_error_is_not_retried(self):
        dependency = {"name": "example", "url": "https://example.test/fixed"}
        forbidden = urllib.error.HTTPError(dependency["url"], 403, "Forbidden", {}, None)
        with patch("build_dependencies.urllib.request.urlopen", side_effect=forbidden) as request:
            with self.assertRaisesRegex(ValueError, "example source download failed: HTTP 403"):
                build_dependencies.download_archive(dependency, Path("unused"))
            self.assertEqual(request.call_count, 1)
