"""Emotion manifest validation tests (plan section 13.3)."""

from __future__ import annotations

import copy
import unittest

from core.emotions import (
    BUNDLED_EMOTIONS_FILE,
    EmotionsManifestError,
    load_emotions_manifest,
    validate_emotions_manifest,
)


class ManifestTest(unittest.TestCase):
    def test_bundled_manifest_loads_and_covers_palette(self):
        manifest = load_emotions_manifest()
        self.assertEqual(manifest["version"], 1)
        names = [e["name"] for e in manifest["emotions"]]
        self.assertEqual(len(names), 18)
        self.assertEqual(len(set(names)), 18)

    def test_unknown_emotion_rejected(self):
        manifest = load_emotions_manifest()
        broken = copy.deepcopy(manifest)
        broken["emotions"][0]["name"] = "ecstatic"
        with self.assertRaises(EmotionsManifestError):
            validate_emotions_manifest(broken)

    def test_unknown_status_emotion_rejected(self):
        manifest = load_emotions_manifest()
        broken = copy.deepcopy(manifest)
        broken["status_emotions"] = ["thinking", "plotting"]
        with self.assertRaises(EmotionsManifestError):
            validate_emotions_manifest(broken)

    def test_missing_fields_rejected(self):
        with self.assertRaises(EmotionsManifestError):
            validate_emotions_manifest({"version": 1, "emotions": [{"name": "neutral"}],
                                        "status_emotions": []})
        with self.assertRaises(EmotionsManifestError):
            validate_emotions_manifest(["not", "a", "dict"])
        with self.assertRaises(EmotionsManifestError):
            validate_emotions_manifest({"emotions": [], "status_emotions": []})

    def test_missing_file_raises_clear_error(self):
        with self.assertRaises(EmotionsManifestError):
            load_emotions_manifest("/nonexistent/emotions.json")


if __name__ == "__main__":
    unittest.main()
