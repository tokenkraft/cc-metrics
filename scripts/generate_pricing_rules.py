#!/usr/bin/env python3
"""Generate Prometheus price gauges from tracked OpenAI pricing data."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "pricing" / "openai-model-pricing.json"
DEFAULT_RULES = ROOT / "prometheus-rules" / "ai-unified.yml"
BEGIN_MARKER = "      # BEGIN GENERATED OPENAI PRICE RULES"
END_MARKER = "      # END GENERATED OPENAI PRICE RULES"
PRICE_TYPES = ("input", "cacheRead", "cacheCreation", "output")
REQUIRED_MODEL_FIELDS = {
    "model",
    "pricing_tier",
    "prices",
    "official_url",
    "verified_at",
    "effective_from",
    "effective_to",
}


class ManifestError(ValueError):
    """Pricing manifest failed validation."""


def _require_date(value: Any, field: str, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if not isinstance(value, str):
        raise ManifestError(f"{field} must be an ISO 8601 date")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ManifestError(f"{field} must be an ISO 8601 date") from exc


def validate_manifest(data: Any) -> dict[str, Any]:
    """Validate manifest shape and return it with narrowed top-level type."""
    if not isinstance(data, dict):
        raise ManifestError("manifest root must be an object")
    if data.get("schema_version") != 1:
        raise ManifestError("schema_version must be 1")
    if data.get("currency") != "USD":
        raise ManifestError("currency must be USD")
    if data.get("unit") != "per_million_tokens":
        raise ManifestError("unit must be per_million_tokens")

    models = data.get("models")
    if not isinstance(models, list) or not models:
        raise ManifestError("models must be a non-empty array")

    seen: set[str] = set()
    for index, entry in enumerate(models):
        label = f"models[{index}]"
        if not isinstance(entry, dict):
            raise ManifestError(f"{label} must be an object")
        missing = REQUIRED_MODEL_FIELDS - entry.keys()
        if missing:
            raise ManifestError(f"{label} missing fields: {', '.join(sorted(missing))}")

        model = entry["model"]
        if not isinstance(model, str) or not model:
            raise ManifestError(f"{label}.model must be a non-empty string")
        if model in seen:
            raise ManifestError(f"duplicate model: {model}")
        seen.add(model)

        pricing_tier = entry["pricing_tier"]
        if pricing_tier not in {"standard", "standard_short_context"}:
            raise ManifestError(f"{label}.pricing_tier is unsupported")

        source = entry["official_url"]
        if not isinstance(source, str):
            raise ManifestError(f"{label}.official_url must be a URL")
        parsed = urlparse(source)
        if parsed.scheme != "https" or parsed.hostname != "developers.openai.com":
            raise ManifestError(f"{label}.official_url must use official OpenAI docs")

        _require_date(entry["verified_at"], f"{label}.verified_at")
        _require_date(entry["effective_from"], f"{label}.effective_from", nullable=True)
        _require_date(entry["effective_to"], f"{label}.effective_to", nullable=True)
        if (
            entry["effective_from"] is not None
            and entry["effective_to"] is not None
            and entry["effective_from"] > entry["effective_to"]
        ):
            raise ManifestError(f"{label} effective date range is inverted")

        prices = entry["prices"]
        if not isinstance(prices, dict):
            raise ManifestError(f"{label}.prices must be an object")
        if "input" not in prices or "output" not in prices:
            raise ManifestError(f"{label}.prices requires input and output")
        unknown_types = prices.keys() - set(PRICE_TYPES)
        if unknown_types:
            raise ManifestError(
                f"{label}.prices has unsupported types: {', '.join(sorted(unknown_types))}"
            )
        for price_type, raw_price in prices.items():
            if not isinstance(raw_price, str):
                raise ManifestError(
                    f"{label}.prices.{price_type} must be a decimal string"
                )
            try:
                value = Decimal(raw_price)
            except InvalidOperation as exc:
                raise ManifestError(
                    f"{label}.prices.{price_type} must be a decimal string"
                ) from exc
            if not value.is_finite() or value <= 0:
                raise ManifestError(f"{label}.prices.{price_type} must be positive")

    return data


def load_manifest(path: Path) -> dict[str, Any]:
    """Load and validate one pricing manifest."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot load {path}: {exc}") from exc
    return validate_manifest(data)


def render_rules(data: dict[str, Any]) -> str:
    """Render deterministic YAML rule block."""
    lines = [
        BEGIN_MARKER,
        "      # Generated by scripts/generate_pricing_rules.py from",
        "      # pricing/openai-model-pricing.json. Do not edit this block.",
    ]
    for entry in data["models"]:
        effective_from = entry["effective_from"] or "unknown"
        effective_to = entry["effective_to"] or "open"
        lines.extend(
            [
                "",
                f"      # {entry['model']}: verified {entry['verified_at']}; "
                f"effective {effective_from} through {effective_to}.",
                f"      # Source: {entry['official_url']}",
            ]
        )
        for price_type in PRICE_TYPES:
            if price_type not in entry["prices"]:
                continue
            lines.extend(
                [
                    "      - record: ai_model_token_price_usd_per_million",
                    f"        expr: vector({entry['prices'][price_type]})",
                    "        labels: "
                    f"{{source: codex, model: {entry['model']}, type: {price_type}, "
                    "cost_kind: api_estimate, "
                    f"pricing_tier: {entry['pricing_tier']}}}",
                ]
            )
    lines.append(END_MARKER)
    return "\n".join(lines)


def replace_generated_block(rules_text: str, generated: str) -> str:
    """Replace exactly one generated block."""
    if rules_text.count(BEGIN_MARKER) != 1 or rules_text.count(END_MARKER) != 1:
        raise ManifestError(
            "rules file must contain exactly one generated pricing block"
        )
    before, remainder = rules_text.split(BEGIN_MARKER, 1)
    _, after = remainder.split(END_MARKER, 1)
    return before + generated + after


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--rules", type=Path, default=DEFAULT_RULES)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true")
    action.add_argument("--write", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        generated = render_rules(load_manifest(args.manifest))
        if not args.check and not args.write:
            print(generated)
            return 0
        current = args.rules.read_text(encoding="utf-8")
        expected = replace_generated_block(current, generated)
        if args.check:
            if current != expected:
                print(
                    "pricing rules are stale; run "
                    "python3 scripts/generate_pricing_rules.py --write",
                    file=sys.stderr,
                )
                return 1
            return 0
        args.rules.write_text(expected, encoding="utf-8")
    except (ManifestError, OSError) as exc:
        print(f"pricing generation failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
