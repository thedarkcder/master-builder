from __future__ import annotations

import unittest

from orchestrator.core.release_train import (
    apply_release_label,
    next_release_version_from_tags,
    normalize_release_version,
    release_label_for_version,
)


class ReleaseTrainTests(unittest.TestCase):
    def test_normalize_release_version_accepts_plain_semver(self) -> None:
        self.assertEqual(normalize_release_version("1.2.3"), "v1.2.3")

    def test_normalize_release_version_rejects_invalid(self) -> None:
        with self.assertRaises(ValueError):
            normalize_release_version("v1.2")

    def test_next_release_version_defaults_when_no_tags(self) -> None:
        self.assertEqual(next_release_version_from_tags([]), "v0.1.0")

    def test_next_release_version_bumps_latest_patch(self) -> None:
        tags = ["v0.1.7", "v0.1.10", "v0.0.9", "bad"]
        self.assertEqual(next_release_version_from_tags(tags), "v0.1.11")

    def test_release_label_for_version(self) -> None:
        self.assertEqual(release_label_for_version("v0.2.0"), "release:v0.2.0")

    def test_apply_release_label_replaces_previous_release_labels(self) -> None:
        labels = ["backend", "release:v0.1.0", "triage"]
        self.assertEqual(
            apply_release_label(labels, target_label="release:v0.1.1"),
            ["backend", "triage", "release:v0.1.1"],
        )


if __name__ == "__main__":
    unittest.main()
