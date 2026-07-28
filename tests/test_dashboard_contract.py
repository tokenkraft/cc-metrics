from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "grafana" / "dashboards" / "ai-monitor.json"


def walk_panels(panels: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    for panel in panels:
        yield panel
        nested = panel.get("panels")
        if isinstance(nested, list):
            yield from walk_panels(nested)


class DashboardContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.raw = DASHBOARD.read_text(encoding="utf-8")
        cls.dashboard = json.loads(cls.raw)
        cls.panels = {
            panel["id"]: panel for panel in walk_panels(cls.dashboard["panels"])
        }

    def test_removed_untracked_and_unsupported_claims(self) -> None:
        for forbidden in (
            "ai_token_usage_reconstructed_total",
            "Roughly 65%",
            "100% main-thread",
            "internal sidechain",
            'query_source=\\"subagent\\"',
            "0.144.6",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.raw)

    def test_session_and_thread_panels_use_counter_increase(self) -> None:
        session = self.panels[1]
        session_expr = session["targets"][0]["expr"]
        self.assertEqual(session["title"], "Session Starts")
        self.assertIn("increase(ai_session_count_total", session_expr)
        self.assertIn('start_type!="agents_view"', session_expr)

        thread = self.panels[200]
        thread_expr = thread["targets"][0]["expr"]
        self.assertEqual(thread["title"], "Codex Thread Starts")
        self.assertIn("increase(ai_thread_count_total", thread_expr)
        self.assertNotIn("max_over_time", thread_expr)

    def test_invalid_work_unit_ratio_is_absent(self) -> None:
        self.assertNotIn(6, self.panels)
        self.assertNotIn("Tokens / Work Unit", self.raw)

    def test_cost_panels_disclose_estimate_basis(self) -> None:
        for panel_id in (3, 11, 14, 15, 301, 306):
            with self.subTest(panel_id=panel_id):
                panel = self.panels[panel_id]
                combined = f"{panel['title']} {panel.get('description', '')}".lower()
                self.assertIn("estimate", combined)
                self.assertIn("not billing data", combined)

    def test_codex_cost_panels_disclose_unpriced_models(self) -> None:
        for panel_id in (3, 11, 14, 15):
            with self.subTest(panel_id=panel_id):
                description = self.panels[panel_id]["description"].lower()
                self.assertIn("unpriced", description)
                self.assertIn("omitted", description)

    def test_cost_panels_select_explicit_emitter_estimates(self) -> None:
        for panel_id in (3, 11, 14, 15, 301, 306):
            with self.subTest(panel_id=panel_id):
                expression = self.panels[panel_id]["targets"][0]["expr"]
                self.assertIn('cost_kind="emitter_estimate"', expression)
                self.assertNotIn('cost_kind=""', expression)

    def test_cost_panels_do_not_price_normalized_long_context_models(self) -> None:
        normalizer = r"([^\\[]+)(\\[1m\\])?"
        for panel_id in (3, 11, 14, 15, 307):
            with self.subTest(panel_id=panel_id):
                expression = self.panels[panel_id]["targets"][0]["expr"]
                self.assertNotIn(normalizer, expression)

    def test_rate_panels_disclose_trailing_window(self) -> None:
        for panel_id in (10, 11):
            with self.subTest(panel_id=panel_id):
                description = self.panels[panel_id]["description"].lower()
                self.assertIn("trailing 1-hour average", description)
                self.assertIn("expressed per hour", description)

    def test_agent_panels_use_emitted_agent_name_only(self) -> None:
        for panel_id in (300, 301, 304, 305, 306):
            with self.subTest(panel_id=panel_id):
                expressions = " ".join(
                    target["expr"] for target in self.panels[panel_id]["targets"]
                )
                self.assertIn('agent_name!=""', expressions)
                self.assertNotIn("query_source", expressions)

    def test_named_agent_share_uses_claude_only_denominator(self) -> None:
        expression = self.panels[305]["targets"][0]["expr"]
        self.assertEqual(expression.count('source="claude_code"'), 2)
        self.assertEqual(expression.count('source=~"$source"'), 2)

    def test_effort_zero_is_gated_by_source_filter(self) -> None:
        expression = self.panels[25]["targets"][0]["expr"]
        self.assertIn('source="claude_code", source=~"$source"', expression)
        self.assertIn("and on() count(", expression)

    def test_token_descriptions_include_reasoning_output(self) -> None:
        for panel_id in (7, 8):
            with self.subTest(panel_id=panel_id):
                self.assertIn(
                    "reasoning output",
                    self.panels[panel_id]["description"].lower(),
                )

    def test_model_panel_discloses_codex_label_normalization(self) -> None:
        description = self.panels[9]["description"]
        self.assertIn("[1m]", description)
        self.assertIn("normalized", description.lower())

    def test_active_time_description_excludes_idle(self) -> None:
        description = self.panels[24]["description"].lower()
        self.assertIn("cli processing", description)
        self.assertIn("excluding idle", description)

    def test_range_panels_disclose_rolling_windows(self) -> None:
        for panel_id in (14, 16):
            with self.subTest(panel_id=panel_id):
                description = self.panels[panel_id]["description"].lower()
                self.assertIn("rolling", description)
                self.assertNotIn("per interval bucket", description)

    def test_cost_range_discloses_step_local_price_semantics(self) -> None:
        description = self.panels[14]["description"].lower()
        self.assertIn("rate gauge recorded at that step", description)
        self.assertIn("evaluation time", description)

    def test_cost_queries_keep_input_when_cache_series_is_absent(self) -> None:
        for panel_id in (3, 11, 14, 15):
            with self.subTest(panel_id=panel_id):
                expression = self.panels[panel_id]["targets"][0]["expr"]
                self.assertGreaterEqual(
                    expression.count('token_type="input"'),
                    2,
                )
                self.assertIn(
                    'token_type=~"cached_input|cache_write_input"',
                    expression,
                )
                self.assertIn(" * 0)", expression)

    def test_unpriced_codex_token_diagnostic_is_visible(self) -> None:
        panel = self.panels[307]
        expression = panel["targets"][0]["expr"]
        self.assertEqual(panel["title"], "Unpriced Codex Tokens")
        self.assertIn("unless on (model, type)", expression)
        self.assertIn('source="codex", source=~"$source"', expression)
        self.assertIn("label_replace", expression)
        self.assertIn("exact model label", panel["description"].lower())
        self.assertIn("not billing data", panel["description"].lower())


if __name__ == "__main__":
    unittest.main()
