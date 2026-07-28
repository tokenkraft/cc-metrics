from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COLLECTOR = ROOT / "otel-collector-config.yaml"
SUPPORTED_DELTA_METRICS = (
    "^(claude_code[.](token[.]usage|cost[.]usage|commit[.]count|"
    "pull_request[.]count|lines_of_code[.]count|code_edit_tool[.]decision|"
    "active_time[.]total|session[.]count)|"
    "codex[.](turn[.]token_usage|thread[.]started))$"
)


class CollectorConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = COLLECTOR.read_text(encoding="utf-8")

    def test_supported_file_log_receiver_name_is_used(self) -> None:
        self.assertIn("file_log/codex_commits:", self.config)
        self.assertNotIn("filelog/codex_commits", self.config)

    def test_privacy_precedes_delta_conversion(self) -> None:
        pipeline = (
            "[memory_limiter, resource, transform/privacy, "
            "filter/metric_contract, "
            "transform/metric_scope, batch/metrics_aggregate, "
            "groupbyattrs/metrics_compact, transform/delta_aggregate_key, "
            "metrics_transform/delta_aggregate, transform/delta_arrival_time, "
            "deltatocumulative]"
        )
        self.assertIn(pipeline, self.config)

    def test_concurrent_delta_aggregation_is_privacy_safe_and_ordered(self) -> None:
        self.assertIn("transform/metric_scope:", self.config)
        self.assertIn('set(scope.name, "cc-metrics")', self.config)
        self.assertIn('keep_matching_keys(scope.attributes, "a^")', self.config)
        self.assertIn("batch/metrics_aggregate:", self.config)
        self.assertIn("send_batch_max_size: 1024", self.config)
        self.assertIn("groupbyattrs/metrics_compact:", self.config)
        self.assertIn("metrics_transform/delta_aggregate:", self.config)
        self.assertIn(f'IsMatch(metric.name, "{SUPPORTED_DELTA_METRICS}")', self.config)
        self.assertIn('include: ".*"', self.config)

    def test_supported_metric_allowlist_is_consistent_and_production_only(self) -> None:
        occurrences = re.findall(
            r"\^\(claude_code\[\.\].+?codex\[\.\].+?\)\$",
            self.config,
        )
        self.assertEqual(len(occurrences), 1)
        self.assertEqual(set(occurrences), {SUPPORTED_DELTA_METRICS})
        self.assertNotIn("audit[.]delta", self.config)

    def test_unsupported_temporality_is_rejected_before_privacy_compaction(
        self,
    ) -> None:
        self.assertIn("filter/metric_contract:", self.config)
        self.assertIn("not IsMatch(metric.name", self.config)
        self.assertIn(
            "metric.aggregation_temporality != AGGREGATION_TEMPORALITY_DELTA",
            self.config,
        )

    def test_exporter_expiration_exceeds_cumulative_state_lifetime(self) -> None:
        self.assertIn("max_stale: 24h", self.config)
        self.assertIn("metric_expiration: 25h", self.config)

    def test_no_producer_identity_is_exported(self) -> None:
        self.assertNotIn("producer.resource_id", self.config)
        self.assertNotIn("producer.session_id", self.config)
        self.assertNotIn("|cost_kind", self.config)
        self.assertNotIn("|agent_name", self.config)
        self.assertNotIn("|pricing_tier", self.config)
        self.assertNotIn("|source", self.config)

    def test_commit_counter_delta_is_converted_to_cumulative(self) -> None:
        pipeline = "[memory_limiter, transform/privacy, deltatocumulative, batch]"
        self.assertIn(pipeline, self.config)

    def test_commit_log_content_is_removed_before_connector(self) -> None:
        self.assertIn("transform/log_privacy:", self.config)
        self.assertIn('keep_matching_keys(log.attributes, "a^")', self.config)
        self.assertIn('set(log.body, "")', self.config)
        self.assertIn(
            "processors: [memory_limiter, resource, transform/log_privacy, batch]",
            self.config,
        )


if __name__ == "__main__":
    unittest.main()
