"""Quarantine contract for the retired query-search-index capability."""

from __future__ import annotations

import unittest

from pipeline.secondary_capability_guard import (
    SecondaryCapabilityUnavailable,
    denial_payload,
)


class QuerySearchIndexCapabilityTests(unittest.TestCase):
    def test_safe_modes_are_deterministically_unavailable(self) -> None:
        for mode in ("codex", "off"):
            with self.subTest(mode=mode):
                payload = denial_payload("query-search-index", mode)
                self.assertEqual(payload["status"], "unavailable")
                self.assertEqual(payload["mode"], mode)

    def test_other_modes_are_denied(self) -> None:
        with self.assertRaises(SecondaryCapabilityUnavailable) as caught:
            _ = denial_payload("query-search-index", "api")
        self.assertEqual(caught.exception.code, "runtime-mode-denied")


if __name__ == "__main__":
    _ = unittest.main()
