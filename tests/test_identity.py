"""CMYK migration regressions; no native or 4D execution is implied."""
import json
from pathlib import Path
import sys
import unittest
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from ci_config import load_config


class IdentityTests(unittest.TestCase):
    def test_exact_repository_package_and_project_case(self):
        config = load_config(ROOT, initialized=True, repository="miyako/CMYK")
        self.assertEqual(config["repository"], "CMYK")
        self.assertEqual(json.loads((ROOT / "package.json").read_text())["name"], "CMYK")
        self.assertIn("CMYK", [path.name for path in (ROOT / "component").iterdir()])
        project = ROOT / "component/CMYK/Project"
        self.assertIn("CMYK.4DProject", [path.name for path in project.iterdir()])
        with self.assertRaisesRegex(ValueError, "actual repository does not match"):
            load_config(ROOT, initialized=True, repository="miyako/cmyk")

    def test_namespace_and_preserved_class_cli_catalog_uuid(self):
        config = load_config(ROOT, initialized=True)
        self.assertEqual((config["namespace"], config["feature_class"], config["cli"]),
                         ("CMYK", "Converter", "image-cmyk"))
        sources = ROOT / "component/CMYK/Project/Sources"
        catalog = ET.parse(sources / "catalog.4DCatalog").getroot()
        self.assertEqual(catalog.attrib["name"], "CMYK")
        self.assertEqual(catalog.attrib["uuid"], "B14F3854A54B56C3B6D5F79DD3EF402A")
        general = ET.parse(sources / "settings.4DSettings").find(".//general")
        self.assertEqual(general.attrib["component_classStore_name"], "CMYK")
        self.assertTrue((sources / "Classes/Converter.4dm").is_file())

    def test_source_host_dependency_identity_and_path(self):
        host = ROOT / "tests/host"
        dependencies = json.loads((host / "Project/Sources/dependencies.json").read_text())
        self.assertEqual(dependencies["dependencies"], {"CMYK": {}})
        environment = json.loads((host / "environment4d.json").read_text())
        self.assertEqual(environment["dependencies"], {"CMYK": "../../component/CMYK"})
        self.assertEqual((host / environment["dependencies"]["CMYK"]).resolve(),
                         (ROOT / "component/CMYK").resolve())

    def test_current_documentation_and_completion_identity(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertTrue(readme.startswith("# CMYK\n"))
        self.assertIn("`miyako/CMYK`", readme)
        self.assertIn("`CMYK.zip`", readme)
        self.assertIn("`CMYK.4dbase`", readme)
        syntax = (ROOT / "component/CMYK/Resources/en.lproj/syntaxEN.json").read_text()
        self.assertIn("Converter", syntax)
        self.assertNotIn("Greeting", syntax)

    def test_public_examples_and_real_host_use_cmyk_namespace(self):
        for name in ["README.md", "component/CMYK/Documentation/Classes/Converter.md",
                     "tests/host/Project/Sources/Methods/Smoke.4dm"]:
            with self.subTest(path=name):
                source = (ROOT / name).read_text(encoding="utf-8")
                self.assertIn("cs.CMYK.Converter", source)
                self.assertNotIn("cs.ImageCMYK.", source)


if __name__ == "__main__":
    unittest.main()
