import json
import os
import tempfile
import unittest

from engine import config

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)


class ManifestDefaultsTest(unittest.TestCase):
    def test_fallback_defaults_match_manifest(self):
        with open(os.path.join(PLUGIN_DIR, "manifest.json"), encoding="utf-8") as handle:
            manifest = json.load(handle)
        defaults = manifest["barWidget"]["defaults"]
        self.assertEqual(defaults, config.FALLBACK_DEFAULTS)

    def test_schema_covers_every_default(self):
        with open(os.path.join(PLUGIN_DIR, "manifest.json"), encoding="utf-8") as handle:
            manifest = json.load(handle)
        widget = manifest["barWidget"]
        keys = {entry["key"] for entry in widget["schema"]}
        self.assertEqual(keys, set(widget["defaults"]))
        for entry in widget["schema"]:
            self.assertEqual(entry["defaultValue"], widget["defaults"][entry["key"]], entry["key"])


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.paths = config.Paths(PLUGIN_DIR)
        self.settings = config.Settings(config.manifest_defaults(self.paths))

    def test_defaults_come_from_manifest(self):
        self.assertEqual(self.settings.skipBack, 15)
        self.assertEqual(self.settings.searchProvider, "itunes")

    def test_update_coerces_and_ignores_unknown(self):
        changed = self.settings.update({"skipBack": "45", "showTitle": "false", "bogus": 1, "id": "x"})
        self.assertTrue(changed)
        self.assertEqual(self.settings.skipBack, 45)
        self.assertIs(self.settings.showTitle, False)
        self.assertNotIn("bogus", self.settings.as_dict())
        self.assertFalse(self.settings.update({"skipBack": 45, "showTitle": False}))

    def test_locale_derivations(self):
        self.assertEqual(config.country_from_locale("zh_CN.UTF-8"), "CN")
        self.assertEqual(config.country_from_locale("en"), "US")
        self.assertEqual(config.country_from_locale("C.UTF-8"), "US")
        self.assertEqual(config.language_from_locale("ja_JP.UTF-8"), "ja")
        self.assertEqual(config.language_from_locale("POSIX"), "en")
        self.settings.update({}, locale="de_DE.UTF-8")
        self.assertEqual(self.settings.country, "DE")
        self.settings.update({"searchCountry": "gb"})
        self.assertEqual(self.settings.country, "GB")

    def test_unknown_attribute_raises(self):
        with self.assertRaises(AttributeError):
            self.settings.nope  # noqa: B018


class CredentialsTest(unittest.TestCase):
    def test_roundtrip_is_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = config.Paths(PLUGIN_DIR)
            paths.config_dir = tmp
            paths.credentials_path = os.path.join(tmp, "credentials.json")
            config.save_credentials(paths, {"podcastindex": {"key": "k", "secret": "s"}})
            self.assertEqual(oct(os.stat(paths.credentials_path).st_mode & 0o777), "0o600")
            self.assertEqual(config.load_credentials(paths)["podcastindex"]["secret"], "s")


if __name__ == "__main__":
    unittest.main()
