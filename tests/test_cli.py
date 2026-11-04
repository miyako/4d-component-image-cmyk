"""Integration checks for the already-built, configured native executable."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import unittest
import uuid
import zlib


ROOT = Path(__file__).resolve().parents[1]


class CppCLI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        selected = os.environ.get("TEST_CLI_LANGUAGE")
        if selected == "none":
            raise unittest.SkipTest("CLI execution disabled by TEST_CLI_LANGUAGE=none")
        if selected not in (None, "cpp"):
            raise RuntimeError("image-cmyk requires TEST_CLI_LANGUAGE=cpp or none")
        config = json.loads((ROOT / "template.json").read_text(encoding="utf-8"))
        if not config.get("initialized") or config.get("language") != "cpp":
            raise RuntimeError("image-cmyk requires an initialized C++ configuration")
        spec = importlib.util.spec_from_file_location("image_cmyk_build", ROOT / "scripts/build_cli.py")
        # build_cli imports the shared script helpers without a package prefix.
        import sys
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            builder = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(builder)
            native = builder.native_target()
        finally:
            sys.path.pop(0)
        target = os.environ.get("CLI_TARGET", native)
        if target != native:
            raise RuntimeError(
                f"CLI_TARGET={target!r} is not this native host ({native}); "
                "foreign artifacts do not provide native integration evidence"
            )
        build = ROOT / "build" / target
        suffix = ".exe" if os.name == "nt" else ""
        cls.executable = build / (config["cli"] + suffix)
        generator = build / "cmake" / ("image-cmyk-fixtures" + suffix)
        if os.name == "nt" and not generator.is_file():
            generator = build / "cmake" / "Release" / generator.name
        for executable in (cls.executable, generator):
            if not executable.is_file():
                raise RuntimeError(
                    f"required native executable unavailable: {executable}; "
                    f"run python3 scripts/build_cli.py --root . --target {target} first"
                )
        cls.version = config["version"]
        cls.scratch = build / (".python-integration-" + uuid.uuid4().hex)
        cls.scratch.mkdir()
        cls.addClassCleanup(shutil.rmtree, cls.scratch)
        cls.fixtures = cls.scratch / "fixtures"
        result = subprocess.run(
            [str(generator), str(cls.fixtures)], capture_output=True, timeout=30
        )
        if result.returncode:
            raise RuntimeError(f"fixture generation failed: {result!r}")
        for name in ("input.png", "input.tif", "input.jpg", "destination.icc"):
            if not (cls.fixtures / name).is_file():
                raise RuntimeError("missing generated fixture: " + name)

    def setUp(self):
        self.directory = self.scratch / uuid.uuid4().hex
        self.directory.mkdir()

    def invoke(self, arguments, executable=None):
        return subprocess.run(
            [os.fspath(executable or self.executable), *map(os.fspath, arguments)],
            capture_output=True, timeout=30, cwd=ROOT,
        )

    def decode_line(self, stream):
        self.assertTrue(stream.endswith(b"\n"), repr(stream))
        self.assertNotIn(b"\r", stream)
        self.assertEqual(len(stream.splitlines()), 1)
        value = json.loads(stream.decode("utf-8", errors="strict"))
        self.assertIsInstance(value, dict)
        self.assertIs(type(value.get("protocol")), int)
        self.assertEqual(value["protocol"], 1)
        return value

    def success(self, arguments, executable=None):
        result = self.invoke(arguments, executable)
        self.assertEqual(result.returncode, 0, repr(result))
        self.assertEqual(result.stderr, b"")
        return self.decode_line(result.stdout)

    def failure(self, arguments, code=None):
        result = self.invoke(arguments)
        self.assertEqual(result.returncode, 2, repr(result))
        self.assertEqual(result.stdout, b"")
        value = self.decode_line(result.stderr)
        self.assertEqual(set(value), {"protocol", "error"})
        self.assertIsInstance(value["error"], dict)
        self.assertEqual(set(value["error"]), {"code", "message"})
        for key in ("code", "message"):
            self.assertIsInstance(value["error"][key], str)
            self.assertTrue(value["error"][key])
        self.assertNotEqual(value["error"]["code"], "internal_error")
        if code is not None:
            self.assertEqual(value["error"]["code"], code)
        return value

    def conversion(self, source=None, destination=None, profile=None):
        return [
            "convert", "--source", source or self.fixtures / "input.png",
            "--destination", destination or self.directory / "output.tif",
            "--destination-profile", profile or self.fixtures / "destination.icc",
        ]

    def inspect(self, source):
        return self.success(["inspect", "--source", source])

    def test_read_errors_identify_source_and_profile_paths(self):
        source, _ = self.untagged_png_and_profile()
        missing = self.directory / "missing profil \u65e5\u672c 'quoted'.icc"
        original = (self.fixtures / "input.png").read_bytes()
        cases = [
            ("source", ["inspect", "--source", missing]),
            ("sourceProfile", self.conversion(source=source) + ["--source-profile", missing]),
            ("destinationProfile", self.conversion(profile=missing)),
            ("destinationProfile", self.conversion(profile=self.directory)),
        ]
        for role, arguments in cases:
            with self.subTest(role=role, arguments=arguments):
                result = self.failure(arguments, "read_failed")
                message = result["error"]["message"]
                self.assertTrue(message.startswith(role + ": "), message)
                expected = self.directory if arguments[-1] == self.directory else missing
                self.assertIn(os.fspath(expected), message)
                self.assertFalse((self.directory / "output.tif").exists())
        self.assertEqual((self.fixtures / "input.png").read_bytes(), original)

    @unittest.skipIf(os.name == "nt", "POSIX file-mode permission probe")
    def test_permission_error_preserves_os_diagnostic(self):
        if os.geteuid() == 0:
            self.skipTest("root can read files regardless of POSIX permission bits")
        denied = self.directory / "denied.png"
        shutil.copyfile(self.fixtures / "input.png", denied)
        denied.chmod(0)
        try:
            value = self.failure(["inspect", "--source", denied], "read_failed")
            message = value["error"]["message"]
            self.assertTrue(message.startswith("source: cannot open file "), message)
            self.assertIn(os.fspath(denied), message)
            self.assertRegex(message, r"OS error [a-z]+:[1-9][0-9]*")
        finally:
            denied.chmod(0o600)

    def assert_image(self, value, color_space, image_format):
        self.assertIs(type(value["width"]), int)
        self.assertIs(type(value["height"]), int)
        self.assertGreater(value["width"], 0)
        self.assertGreater(value["height"], 0)
        self.assertEqual(value["colorSpace"], color_space)
        self.assertEqual(value["channels"], 4 if color_space == "CMYK" else 3)
        self.assertEqual(value["format"], image_format)
        self.assertEqual(value["bitDepth"], 8)
        self.assertIs(value["alpha"], False)
        self.assertEqual(value["orientation"], 1)
        for field in ("dpiX", "dpiY"):
            self.assertIsInstance(value[field], (int, float))
            self.assertNotIsInstance(value[field], bool)
            self.assertGreaterEqual(value[field], 0)
        self.assertIs(type(value["embeddedProfile"]["present"]), bool)
        if value["embeddedProfile"]["present"]:
            self.assertEqual(value["embeddedProfile"]["colorSpace"], color_space)

    def test_version(self):
        self.assertEqual(self.success(["--version"]), {"protocol": 1, "version": self.version})

    def test_inspect_tagged_inputs(self):
        for name, image_format in (("input.png", "png"), ("input.tif", "tiff"), ("input.jpg", "jpeg")):
            with self.subTest(name=name):
                value = self.inspect(self.fixtures / name)
                self.assert_image(value, "RGB", image_format)
                self.assertIs(value["embeddedProfile"]["present"], True)

    def test_convert_all_input_formats_and_preserve_sources(self):
        for name in ("input.png", "input.tif", "input.jpg"):
            for output_format in ("tiff", "jpeg"):
                with self.subTest(source=name, output=output_format):
                    source = self.fixtures / name
                    before = hashlib.sha256(source.read_bytes()).digest()
                    destination = self.directory / (name + "." + output_format)
                    result = self.success(self.conversion(source, destination) + ["--format", output_format])
                    self.assertEqual(result["colorSpace"], "CMYK")
                    self.assertEqual(result["channels"], 4)
                    self.assertEqual(result["format"], output_format)
                    self.assertEqual(result["sourceProfile"]["colorSpace"], "RGB")
                    self.assertEqual(result["destinationProfile"]["colorSpace"], "CMYK")
                    self.assertEqual(result["warnings"], [])
                    info = self.inspect(destination)
                    self.assert_image(info, "CMYK", output_format)
                    self.assertIs(info["embeddedProfile"]["present"], True)
                    self.assertEqual((info["width"], info["height"]), (result["width"], result["height"]))
                    self.assertEqual(hashlib.sha256(source.read_bytes()).digest(), before)

    def test_defaults_match_explicit_options(self):
        default = self.directory / "default.tif"
        explicit = self.directory / "explicit.tif"
        self.success(self.conversion(destination=default))
        self.success(self.conversion(destination=explicit) + [
            "--format", "tiff", "--quality", "90", "--black-point-compensation", "true",
            "--intent", "relativeColorimetric", "--allow-assume-srgb", "false",
        ])
        self.assertEqual(default.read_bytes(), explicit.read_bytes())

    def untagged_png_and_profile(self):
        tagged = (self.fixtures / "input.png").read_bytes()
        self.assertEqual(tagged[:8], b"\x89PNG\r\n\x1a\n")
        chunks = [tagged[:8]]
        profiles = []
        offset = 8
        while offset < len(tagged):
            length = struct.unpack_from(">I", tagged, offset)[0]
            chunk_type = tagged[offset + 4:offset + 8]
            chunk = tagged[offset:offset + length + 12]
            if chunk_type == b"iCCP":
                payload = chunk[8:-4]
                separator = payload.index(b"\x00")
                self.assertEqual(payload[separator + 1], 0)
                profiles.append(zlib.decompress(payload[separator + 2:]))
            else:
                chunks.append(chunk)
            offset += length + 12
        self.assertEqual(len(profiles), 1)
        source = self.directory / "untagged.png"
        profile = self.directory / "source RGB.icc"
        source.write_bytes(b"".join(chunks))
        profile.write_bytes(profiles[0])
        return source, profile

    def test_source_profile_selection_and_explicit_assumption(self):
        source, rgb_profile = self.untagged_png_and_profile()
        original = source.read_bytes()
        self.assertIs(self.inspect(source)["embeddedProfile"]["present"], False)
        self.failure(self.conversion(source=source), "missing_source_profile")
        self.failure(self.conversion(source=source) + ["--allow-assume-srgb", "false"],
                     "missing_source_profile")
        self.assertFalse((self.directory / "output.tif").exists())
        assumed = self.success(self.conversion(source, self.directory / "assumed.tif") + [
            "--allow-assume-srgb", "true",
        ])
        self.assertEqual(assumed["sourceProfile"]["colorSpace"], "RGB")
        self.assertIsInstance(assumed["warnings"], list)
        self.assertTrue(assumed["warnings"])
        self.assertTrue(all(isinstance(value, str) and value for value in assumed["warnings"]))
        supplied = self.success(self.conversion(source, self.directory / "supplied.tif") + [
            "--source-profile", rgb_profile,
        ])
        self.assertEqual(supplied["sourceProfile"]["colorSpace"], "RGB")
        self.assertEqual(supplied["warnings"], [])
        self.failure(self.conversion() + ["--source-profile", rgb_profile], "profile_override")
        self.failure(self.conversion(source=source) + [
            "--source-profile", self.fixtures / "destination.icc",
        ], "profile_mismatch")
        self.failure(self.conversion(profile=rgb_profile), "profile_mismatch")
        malformed = self.directory / "bad-source.icc"
        malformed.write_bytes(b"invalid profile")
        self.failure(self.conversion(source=source) + ["--source-profile", malformed], "invalid_profile")
        self.assertEqual(source.read_bytes(), original)
        self.assertFalse((self.directory / "output.tif").exists())

    def test_cmyk_source_profile_and_roundtrip(self):
        tagged = self.directory / "tagged-cmyk.jpg"
        self.success(self.conversion(destination=tagged) + ["--format", "jpeg"])
        converted = self.directory / "converted-cmyk.tif"
        result = self.success(self.conversion(tagged, converted))
        self.assertEqual(result["sourceProfile"]["colorSpace"], "CMYK")
        self.assert_image(self.inspect(converted), "CMYK", "tiff")
        self.failure(self.conversion(tagged, self.directory / "override.tif") + [
            "--source-profile", self.fixtures / "destination.icc",
        ], "profile_override")
        # Remove only JPEG APP2 ICC segments, retaining the original compressed pixels.
        data = tagged.read_bytes()
        self.assertEqual(data[:2], b"\xff\xd8")
        segments = [data[:2]]
        offset = 2
        removed = 0
        profile_parts = {}
        while data[offset:offset + 2] != b"\xff\xda":
            self.assertEqual(data[offset], 0xff)
            length = struct.unpack_from(">H", data, offset + 2)[0]
            segment = data[offset:offset + length + 2]
            if segment[:2] == b"\xff\xe2" and segment[4:16] == b"ICC_PROFILE\x00":
                removed += 1
                self.assertNotIn(segment[16], profile_parts)
                profile_parts[segment[16]] = segment[18:]
            else:
                segments.append(segment)
            offset += length + 2
        self.assertGreater(removed, 0)
        self.assertEqual(
            b"".join(profile_parts[index] for index in range(1, removed + 1)),
            (self.fixtures / "destination.icc").read_bytes(),
        )
        segments.append(data[offset:])
        untagged = self.directory / "untagged-cmyk.jpg"
        untagged.write_bytes(b"".join(segments))
        self.assertIs(self.inspect(untagged)["embeddedProfile"]["present"], False)
        self.failure(self.conversion(untagged, self.directory / "missing-profile.tif"),
                     "missing_source_profile")
        self.failure(self.conversion(untagged, self.directory / "assumed-cmyk.tif") + [
            "--allow-assume-srgb", "true",
        ], "missing_source_profile")
        supplied = self.directory / "supplied-cmyk.tif"
        self.success(self.conversion(untagged, supplied) + [
            "--source-profile", self.fixtures / "destination.icc",
        ])
        self.assert_image(self.inspect(supplied), "CMYK", "tiff")

    def test_intents_and_jpeg_quality(self):
        for index, intent in enumerate(("relativeColorimetric", "perceptual", "saturation", "absoluteColorimetric")):
            with self.subTest(intent=intent):
                destination = self.directory / (str(index) + ".jpg")
                self.success(self.conversion(destination=destination) + [
                    "--intent", intent, "--black-point-compensation", "false",
                    "--format", "jpeg", "--quality", "1" if index % 2 else "100",
                ])
                self.assert_image(self.inspect(destination), "CMYK", "jpeg")

    def test_no_clobber_or_source_mutation(self):
        source = self.directory / "input.png"
        shutil.copy2(self.fixtures / "input.png", source)
        original = source.read_bytes()
        destination = self.directory / "existing.tif"
        destination.write_bytes(b"existing destination must survive")
        self.failure(self.conversion(source, destination), "destination_exists")
        self.assertEqual(destination.read_bytes(), b"existing destination must survive")
        self.failure(self.conversion(source, source), "destination_exists")
        self.assertEqual(source.read_bytes(), original)
        if os.name == "posix":
            alias = self.directory / "alias.tif"
            alias.symlink_to(source)
            self.failure(self.conversion(source, alias), "destination_exists")
            self.assertTrue(alias.is_symlink())
            self.assertEqual(source.read_bytes(), original)

    def test_missing_and_malformed_files_and_profiles(self):
        malformed = self.directory / "malformed"
        malformed.write_bytes(b"not an image or ICC profile\x00\xff")
        empty = self.directory / "empty"
        empty.write_bytes(b"")
        missing = self.directory / "missing"
        for source in (malformed, empty, missing, self.directory):
            with self.subTest(source=source):
                self.failure(["inspect", "--source", source])
                self.failure(self.conversion(source=source))
                self.assertFalse((self.directory / "output.tif").exists())
        for profile in (malformed, empty, missing, self.directory):
            with self.subTest(profile=profile):
                self.failure(self.conversion(profile=profile))
                self.assertFalse((self.directory / "output.tif").exists())
        truncated = self.directory / "truncated.png"
        truncated.write_bytes((self.fixtures / "input.png").read_bytes()[:24])
        self.failure(["inspect", "--source", truncated])

    def test_unsupported_alpha_grayscale_and_bit_depth(self):
        def chunk(kind, payload):
            return (struct.pack(">I", len(payload)) + kind + payload
                    + struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff))

        for name, depth, model, pixels in (
            ("rgba", 8, 6, b"\x10\x20\x30\xff"),
            ("gray", 8, 0, b"\x40"),
            ("rgb16", 16, 2, b"\x10\x00\x20\x00\x30\x00"),
        ):
            with self.subTest(name=name):
                source = self.directory / (name + ".png")
                source.write_bytes(
                    b"\x89PNG\r\n\x1a\n"
                    + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, depth, model, 0, 0, 0))
                    + chunk(b"IDAT", zlib.compress(b"\x00" + pixels))
                    + chunk(b"IEND", b"")
                )
                self.failure(["inspect", "--source", source], "unsupported_image")
                self.failure(self.conversion(source=source) + ["--allow-assume-srgb", "true"],
                             "unsupported_image")
                self.assertFalse((self.directory / "output.tif").exists())

    def test_unsupported_tiff_orientation(self):
        data = bytearray((self.fixtures / "input.tif").read_bytes())
        byte_order = "<" if data[:2] == b"II" else ">"
        self.assertEqual(struct.unpack_from(byte_order + "H", data, 2)[0], 42)
        directory = struct.unpack_from(byte_order + "I", data, 4)[0]
        count = struct.unpack_from(byte_order + "H", data, directory)[0]
        found = False
        for index in range(count):
            entry = directory + 2 + index * 12
            tag, field_type, field_count = struct.unpack_from(byte_order + "HHI", data, entry)
            if tag == 274:
                self.assertEqual((field_type, field_count), (3, 1))
                struct.pack_into(byte_order + "H", data, entry + 8, 6)
                found = True
                break
        self.assertTrue(found, "fixture must explicitly carry TIFF orientation")
        source = self.directory / "rotated.tif"
        source.write_bytes(data)
        self.failure(["inspect", "--source", source], "unsupported_orientation")
        self.failure(self.conversion(source=source), "unsupported_orientation")
        self.assertFalse((self.directory / "output.tif").exists())

    def test_invalid_options(self):
        for arguments in (
            [], [""], ["unknown"], ["--version", "extra"], ["--version", "--version"],
            ["inspect"], ["inspect", "--source"], ["inspect", "--source", ""],
            ["inspect", "--source", self.fixtures / "input.png", "--source", self.fixtures / "input.png"],
            ["inspect", "--destination", "out"], ["convert"], ["convert", "--source", "in"],
        ):
            with self.subTest(arguments=arguments):
                self.failure(arguments, "invalid_arguments")
        for option, values in {
            "--format": ("png", "TIFF", ""),
            "--quality": ("0", "101", "-1", "1.5", "90x", "true"),
            "--intent": ("unknown", "RelativeColorimetric"),
            "--black-point-compensation": ("True", "0", "yes"),
            "--allow-assume-srgb": ("False", "1", "yes"),
            "--unknown": ("value",),
        }.items():
            for value in values:
                with self.subTest(option=option, value=value):
                    self.failure(self.conversion() + [option, value], "invalid_arguments")
                    self.assertFalse((self.directory / "output.tif").exists())

    def test_paths_and_renamed_executable(self):
        names = ["two words", "世界 café 😀", "apostrophe's", "shell;$()&"]
        if os.name == "posix":
            names += ['double"quote', "back\\slash"]
        for name in names:
            with self.subTest(name=name):
                directory = self.directory / name
                directory.mkdir()
                source = directory / "source.png"
                profile = directory / "destination profile.icc"
                destination = directory / "output file.data"
                executable = directory / self.executable.name
                shutil.copy2(self.fixtures / "input.png", source)
                shutil.copy2(self.fixtures / "destination.icc", profile)
                shutil.copy2(self.executable, executable)
                result = self.success(self.conversion(source, destination, profile), executable)
                self.assertEqual(result["format"], "tiff")
                self.assert_image(self.inspect(destination), "CMYK", "tiff")

    @unittest.skipUnless(os.name == "posix", "raw byte arguments require POSIX")
    def test_malformed_utf8(self):
        for value in (b"\x80", b"\xff", b"\xc0\xaf", b"\xed\xa0\x80", b"\xf4\x90\x80\x80"):
            with self.subTest(value=value):
                self.failure([b"inspect", b"--source", value], "invalid_arguments")


if __name__ == "__main__":
    unittest.main()
