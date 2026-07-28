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
MANIFEST = ROOT / "pricing" / "openai-model-pricing.json"
RULES = ROOT / "prometheus-rules" / "ai-unified.yml"


def load_generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("generate_pricing_rules", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def manifest_data() -> dict[str, Any]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


class PricingRulesTests(unittest.TestCase):
    def test_manifest_and_generated_rules_are_current(self) -> None:
        generator = load_generator()
        generated = generator.render_rules(generator.load_manifest(MANIFEST))
        current = RULES.read_text(encoding="utf-8")

        self.assertEqual(generator.replace_generated_block(current, generated), current)

    def test_manifest_rejects_duplicate_models(self) -> None:
        generator = load_generator()
        data = manifest_data()
        data["models"].append(copy.deepcopy(data["models"][0]))

        with self.assertRaisesRegex(generator.ManifestError, "duplicate model"):
            generator.validate_manifest(data)

    def test_manifest_rejects_non_official_source(self) -> None:
        generator = load_generator()
        data = manifest_data()
        data["models"][0]["official_url"] = "https://example.com/pricing"

        with self.assertRaisesRegex(generator.ManifestError, "official OpenAI docs"):
            generator.validate_manifest(data)

    def test_manifest_rejects_missing_provenance_field(self) -> None:
        generator = load_generator()
        data = manifest_data()
        del data["models"][0]["verified_at"]

        with self.assertRaisesRegex(generator.ManifestError, "missing fields"):
            generator.validate_manifest(data)

    def test_gpt_5_3_codex_uses_specialized_pricing_provenance(self) -> None:
        data = manifest_data()
        entry = next(
            model for model in data["models"] if model["model"] == "gpt-5.3-codex"
        )

        self.assertEqual(
            entry["official_url"],
            "https://developers.openai.com/api/docs/pricing#specialized-models",
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
