from __future__ import annotations

import copy
import importlib.util
import json
import re
import unittest
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "generate_pricing_rules.py"
RULES = ROOT / "prometheus-rules" / "ai-unified.yml"


def load_generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("generate_pricing_rules", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GENERATOR = load_generator()


def manifest_path(provider: str) -> Path:
    return ROOT / GENERATOR.PROVIDERS[provider]["manifest"]


def manifest_data(provider: str) -> dict[str, Any]:
    return json.loads(manifest_path(provider).read_text(encoding="utf-8"))


class PricingRulesTests(unittest.TestCase):
    def test_manifests_and_generated_rules_are_current(self) -> None:
        current = RULES.read_text(encoding="utf-8")

        self.assertEqual(
            GENERATOR.render_all(current, sorted(GENERATOR.PROVIDERS)), current
        )

    def test_every_provider_manifest_validates(self) -> None:
        for provider in GENERATOR.PROVIDERS:
            with self.subTest(provider=provider):
                GENERATOR.load_manifest(manifest_path(provider), provider)

    def test_manifest_rejects_duplicate_models(self) -> None:
        for provider in GENERATOR.PROVIDERS:
            with self.subTest(provider=provider):
                data = manifest_data(provider)
                data["models"].append(copy.deepcopy(data["models"][0]))

                with self.assertRaisesRegex(GENERATOR.ManifestError, "duplicate model"):
                    GENERATOR.validate_manifest(data, provider)

    def test_manifest_rejects_non_official_source(self) -> None:
        for provider, docs_name in (
            ("openai", "official OpenAI docs"),
            ("xai", "official xAI docs"),
        ):
            with self.subTest(provider=provider):
                data = manifest_data(provider)
                data["models"][0]["official_url"] = "https://example.com/pricing"

                with self.assertRaisesRegex(GENERATOR.ManifestError, docs_name):
                    GENERATOR.validate_manifest(data, provider)

    def test_manifest_rejects_missing_provenance_field(self) -> None:
        data = manifest_data("openai")
        del data["models"][0]["verified_at"]

        with self.assertRaisesRegex(GENERATOR.ManifestError, "missing fields"):
            GENERATOR.validate_manifest(data, "openai")

    def test_provider_blocks_pin_their_source_label(self) -> None:
        for provider, source_label in (("openai", "codex"), ("xai", "grok_code")):
            with self.subTest(provider=provider):
                rendered = GENERATOR.render_rules(
                    GENERATOR.load_manifest(manifest_path(provider), provider),
                    provider,
                )
                labels = re.findall(r"labels: \{source: ([^,]+),", rendered)
                self.assertTrue(labels)
                self.assertEqual(set(labels), {source_label})

    def test_no_model_is_priced_by_more_than_one_provider(self) -> None:
        seen: dict[str, str] = {}
        for provider in GENERATOR.PROVIDERS:
            for entry in manifest_data(provider)["models"]:
                model = entry["model"]
                self.assertNotIn(
                    model,
                    seen,
                    f"{model} priced by both {seen.get(model)} and {provider}; "
                    "the by-model cost panel sums by (model) and would merge "
                    "both providers into one mislabeled slice",
                )
                seen[model] = provider

    def test_gpt_5_3_codex_uses_specialized_pricing_provenance(self) -> None:
        data = manifest_data("openai")
        entry = next(
            model for model in data["models"] if model["model"] == "gpt-5.3-codex"
        )

        self.assertEqual(
            entry["official_url"],
            "https://developers.openai.com/api/docs/pricing#specialized-models",
        )

    def test_grok_4_6_uses_short_context_tier_pricing(self) -> None:
        data = manifest_data("xai")
        entry = next(model for model in data["models"] if model["model"] == "grok-4.6")

        self.assertEqual(entry["pricing_tier"], "standard_short_context")
        self.assertEqual(
            entry["prices"],
            {"input": "2.00", "cacheRead": "0.50", "output": "6.00"},
        )

    def test_claude_cost_has_explicit_emitter_estimate_provenance(self) -> None:
        rules = RULES.read_text(encoding="utf-8")
        self.assertRegex(
            rules,
            re.compile(
                r"- record: ai_cost_usage_usd_total\n"
                r"\s+expr: claude_code_cost_usage_USD_total\n"
                r"\s+labels:\n"
                r"\s+source: claude_code\n"
                r"\s+cost_kind: emitter_estimate"
            ),
        )


if __name__ == "__main__":
    unittest.main()
