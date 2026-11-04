"""The digest order must not depend on Windows case-insensitive Path ordering."""
import hashlib
from pathlib import Path, PurePosixPath, PureWindowsPath
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from build_provenance import source_digest


class PortableProvenanceTests(unittest.TestCase):
    def test_digest_uses_relative_posix_string_order_for_mixed_case_files(self):
        names = ["cli/a.txt", "cli/Z.txt", "cli/z/B.txt", "cli/z/a.txt"]
        expected = sorted(names)
        for path_type in (PurePosixPath, PureWindowsPath):
            self.assertEqual(sorted((path_type(name) for name in names),
                                    key=lambda path: path.as_posix()),
                             [path_type(name) for name in expected])
        self.assertNotEqual(sorted(PureWindowsPath(name) for name in names),
                            [PureWindowsPath(name) for name in expected])
        with tempfile.TemporaryDirectory(prefix="cmyk-provenance-") as directory:
            root = Path(directory)
            for name in ["template.json", "package.json", *names]:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(name.encode("ascii"))
            digest = hashlib.sha256()
            for name in ["template.json", "package.json", *expected]:
                digest.update(name.encode("ascii") + b"\0" + name.encode("ascii") + b"\0")
            self.assertEqual(source_digest(root), digest.hexdigest())


if __name__ == "__main__":
    unittest.main()
