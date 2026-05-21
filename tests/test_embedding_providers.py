# SPDX-License-Identifier: Apache-2.0
"""Unit tests for V2 — Embedding provider abstraction (ADR 0007).

Pins:
- The 3 providers (local / openai / voyage) all implement EmbeddingProvider.
- `is_available()` correctly detects missing key + missing dep.
- `get_provider()` honors KAHZAABU_EMBED_PROVIDER override.
- `get_provider()` auto-selects in priority order: local → openai → voyage.
- `get_provider()` raises a clear message when nothing is available.
- `report_availability()` mirrors get_provider's selection logic.

Tests do NOT make real API calls. Providers are mocked at the
is_available() and instantiation boundary.

Run:
    .venv/bin/python -m unittest tests.test_embedding_providers
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kahzaabu import embeddings


class ProviderShapeTests(unittest.TestCase):
    """Each provider class must declare model/dim/price_per_m correctly."""

    def test_local_declares_model_and_dim(self):
        self.assertEqual(embeddings.LocalEmbedder.dim, 384)
        self.assertIn("MiniLM",  embeddings.LocalEmbedder.model)
        self.assertEqual(embeddings.LocalEmbedder.price_per_m, 0.0)

    def test_openai_declares_model_and_dim(self):
        self.assertEqual(embeddings.OpenAIEmbedder.dim, 1536)
        self.assertEqual(embeddings.OpenAIEmbedder.model,
                          "text-embedding-3-small")
        self.assertGreater(embeddings.OpenAIEmbedder.price_per_m, 0)

    def test_voyage_declares_model_and_dim(self):
        self.assertEqual(embeddings.VoyageEmbedder.dim, 1024)
        self.assertEqual(embeddings.VoyageEmbedder.model, "voyage-3")
        self.assertGreater(embeddings.VoyageEmbedder.price_per_m, 0)

    def test_PROVIDERS_dict_complete(self):
        self.assertEqual(set(embeddings.PROVIDERS),
                          {"local", "openai", "voyage"})


class IsAvailableTests(unittest.TestCase):
    def test_openai_unavailable_without_key(self):
        with patch.dict("os.environ", {}, clear=False):
            # ensure OPENAI_API_KEY is absent
            import os
            os.environ.pop("OPENAI_API_KEY", None)
            ok, reason = embeddings.OpenAIEmbedder.is_available()
            self.assertFalse(ok)
            self.assertIn("OPENAI_API_KEY", reason)

    def test_voyage_unavailable_without_key(self):
        import os
        os.environ.pop("VOYAGE_API_KEY", None)
        ok, reason = embeddings.VoyageEmbedder.is_available()
        self.assertFalse(ok)
        self.assertIn("VOYAGE_API_KEY", reason)


class GetProviderTests(unittest.TestCase):
    """get_provider() routing logic, with all providers mocked so no API
    keys / models are needed."""

    def _patch(self, available_set: set):
        """Helper: patch is_available so only `available_set` providers
        report ok, and patch __init__ so instantiation is a no-op."""
        stack = []
        for name, cls in embeddings.PROVIDERS.items():
            ok = name in available_set
            stack.append(patch.object(
                cls, "is_available",
                classmethod(lambda c, _ok=ok: (_ok, "ok" if _ok else "stub-missing")),
            ))
            stack.append(patch.object(
                cls, "__init__", lambda self_, _c=cls: None,
            ))
        return stack

    def _enter(self, patches):
        for p in patches:
            p.start()

    def _exit(self, patches):
        for p in patches:
            p.stop()

    def test_explicit_name_argument_wins(self):
        patches = self._patch({"local", "openai"})
        self._enter(patches)
        try:
            p = embeddings.get_provider(name="openai", env={})
            self.assertIsInstance(p, embeddings.OpenAIEmbedder)
        finally:
            self._exit(patches)

    def test_env_var_overrides_auto(self):
        patches = self._patch({"local", "voyage"})
        self._enter(patches)
        try:
            p = embeddings.get_provider(env={"KAHZAABU_EMBED_PROVIDER": "voyage"})
            self.assertIsInstance(p, embeddings.VoyageEmbedder)
        finally:
            self._exit(patches)

    def test_unknown_explicit_provider_raises(self):
        with self.assertRaises(RuntimeError) as cm:
            embeddings.get_provider(name="bogus", env={})
        self.assertIn("Unknown", str(cm.exception))

    def test_explicit_unavailable_provider_raises_with_reason(self):
        patches = self._patch({"local"})  # openai NOT available
        self._enter(patches)
        try:
            with self.assertRaises(RuntimeError) as cm:
                embeddings.get_provider(name="openai", env={})
            self.assertIn("openai", str(cm.exception).lower())
            self.assertIn("stub-missing", str(cm.exception))
        finally:
            self._exit(patches)

    def test_auto_prefers_local_over_openai(self):
        patches = self._patch({"local", "openai"})
        self._enter(patches)
        try:
            p = embeddings.get_provider(env={})
            self.assertIsInstance(p, embeddings.LocalEmbedder)
        finally:
            self._exit(patches)

    def test_auto_falls_through_to_openai_when_local_missing(self):
        patches = self._patch({"openai", "voyage"})
        self._enter(patches)
        try:
            p = embeddings.get_provider(env={})
            self.assertIsInstance(p, embeddings.OpenAIEmbedder)
        finally:
            self._exit(patches)

    def test_no_provider_available_raises_with_help(self):
        patches = self._patch(set())   # nothing available
        self._enter(patches)
        try:
            with self.assertRaises(RuntimeError) as cm:
                embeddings.get_provider(env={})
            self.assertIn("ml-local", str(cm.exception))
            self.assertIn("ml-openai", str(cm.exception))
            self.assertIn("ml-voyage", str(cm.exception))
        finally:
            self._exit(patches)


class ReportAvailabilityTests(unittest.TestCase):
    def test_includes_all_three_providers(self):
        r = embeddings.report_availability(env={})
        self.assertIn("local", r["providers"])
        self.assertIn("openai", r["providers"])
        self.assertIn("voyage", r["providers"])

    def test_each_provider_block_has_model_dim_price(self):
        r = embeddings.report_availability(env={})
        for name, block in r["providers"].items():
            self.assertIn("model", block, name)
            self.assertIn("dim", block, name)
            self.assertIn("price_per_m_tokens", block, name)
            self.assertIn("available", block, name)
            self.assertIn("reason", block, name)

    def test_requested_field_reflects_env(self):
        r = embeddings.report_availability(env={"KAHZAABU_EMBED_PROVIDER": "voyage"})
        self.assertEqual(r["requested"], "voyage")


if __name__ == "__main__":
    unittest.main(verbosity=2)
