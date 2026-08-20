"""Resident runtime seam tests."""

from __future__ import annotations

from types import SimpleNamespace
import unittest

from disco_proxy_soul.app import CompanionApp
from disco_proxy_soul.runtime import ResidentRuntime, build_embedded_runtime


class SyntheticResident:
    async def respond(self, channel_id, user_text, **kwargs):
        return "synthetic reply"


class RuntimeSeamTests(unittest.TestCase):
    def test_embedded_companion_uses_the_public_resident_seam(self) -> None:
        self.assertIsInstance(CompanionApp.__new__(CompanionApp), ResidentRuntime)

    def test_synthetic_resident_uses_the_same_cognition_seam(self) -> None:
        self.assertIsInstance(SyntheticResident(), ResidentRuntime)

    def test_embedded_builder_constructs_exactly_one_runtime(self) -> None:
        config = SimpleNamespace()
        runtime = SimpleNamespace()
        calls = []

        def factory(received):
            calls.append(received)
            return runtime

        built = build_embedded_runtime(config, factory=factory)

        self.assertIs(built, runtime)
        self.assertEqual(calls, [config])


if __name__ == "__main__":
    unittest.main()
