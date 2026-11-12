"""Lightweight unit tests for ARK admin helpers and mock inference (stdlib only)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "admin"))
sys.path.insert(0, str(REPO_ROOT / "tests"))

from mock_inference import deterministic_embedding  # noqa: E402


class TestMockInference(unittest.TestCase):
    def test_deterministic_embedding_dimension_and_norm(self) -> None:
        vec1 = deterministic_embedding("ARK air-gapped test", dim=1024)
        vec2 = deterministic_embedding("ARK air-gapped test", dim=1024)
        vec3 = deterministic_embedding("Different text", dim=1024)
        self.assertEqual(len(vec1), 1024)
        self.assertEqual(vec1, vec2)
        self.assertNotEqual(vec1, vec3)


if __name__ == "__main__":
    unittest.main()
