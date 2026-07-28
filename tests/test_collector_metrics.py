from __future__ import annotations

import http.client
import json
import re
import shutil
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COLLECTOR = ROOT / "otel-collector-config.yaml"
ENV_EXAMPLE = ROOT / ".env.example"
METRIC_NAME = "claude_code_pull_request_count_total"


def collector_image() -> str:
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        if line.startswith("OTEL_COLLECTOR_IMAGE="):
            image = line.partition("=")[2].strip()
            if "@sha256:" not in image:
                raise RuntimeError("OTEL_COLLECTOR_IMAGE must be digest-pinned")
            return image
    raise RuntimeError("OTEL_COLLECTOR_IMAGE is missing from .env.example")


def run_docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        check=check,
        capture_output=True,
        text=True,
        timeout=30,
    )


def otlp_delta_payload(
    *,
    resource_id: str,
    session_id: str,
    email: str,
    account_id: str,
    scope_name: str = "cc-metrics-audit",
    metric_name: str = "claude_code.pull_request.count",
    model: str = "audit-model",
    temporality: int = 1,
    start_ns: int,
    time_ns: int,
    value: int,
) -> bytes:
    attributes = [
        {"key": "model", "value": {"stringValue": model}},
        {"key": "agent.name", "value": {"stringValue": "official-agent"}},
        {"key": "agent_name", "value": {"stringValue": "raw-agent-private"}},
        {"key": "cost_kind", "value": {"stringValue": "raw-cost-private"}},
        {"key": "pricing_tier", "value": {"stringValue": "raw-tier-private"}},
        {"key": "source", "value": {"stringValue": "raw-source-private"}},
        {"key": "session.id", "value": {"stringValue": session_id}},
        {"key": "user.account_uuid", "value": {"stringValue": account_id}},
    ]
    payload = {
        "resourceMetrics": [
            {
                "resource": {
                    "attributes": [
                        {
                            "key": "service.instance.id",
                            "value": {"stringValue": resource_id},
                        },
                        {"key": "user.email", "value": {"stringValue": email}},
                    ]
                },
                "scopeMetrics": [
                    {
                        "scope": {
                            "name": scope_name,
                            "version": scope_name,
                            "attributes": [
                                {
                                    "key": "producer.scope.secret",
                                    "value": {"stringValue": scope_name},
                                }
                            ],
                        },
                        "metrics": [
                            {
                                "name": metric_name,
                                "sum": {
                                    "aggregationTemporality": temporality,
                                    "isMonotonic": True,
                                    "dataPoints": [
                                        {
                                            "attributes": attributes,
                                            "startTimeUnixNano": str(start_ns),
                                            "timeUnixNano": str(time_ns),
                                            "asInt": str(value),
                                        }
                                    ],
                                },
                            }
                        ],
                    }
                ],
            }
        ]
    }
    return json.dumps(payload).encode("utf-8")


def otlp_delta_histogram_payload(
    *,
    resource_id: str,
    session_id: str,
    scope_name: str,
    start_ns: int,
    time_ns: int,
    count: int,
    total: float,
    bucket_counts: list[int],
) -> bytes:
    payload = {
        "resourceMetrics": [
            {
                "resource": {
                    "attributes": [
                        {
                            "key": "service.instance.id",
                            "value": {"stringValue": resource_id},
                        }
                    ]
                },
                "scopeMetrics": [
                    {
                        "scope": {
                            "name": scope_name,
                            "version": scope_name,
                            "attributes": [
                                {
                                    "key": "producer.scope.secret",
                                    "value": {"stringValue": scope_name},
                                }
                            ],
                        },
                        "metrics": [
                            {
                                "name": "codex.turn.token_usage",
                                "histogram": {
                                    "aggregationTemporality": 1,
                                    "dataPoints": [
                                        {
                                            "attributes": [
                                                {
                                                    "key": "model",
                                                    "value": {
                                                        "stringValue": "audit-model"
                                                    },
                                                },
                                                {
                                                    "key": "token_type",
                                                    "value": {"stringValue": "input"},
                                                },
                                                {
                                                    "key": "session.id",
                                                    "value": {
                                                        "stringValue": session_id
                                                    },
                                                },
                                            ],
                                            "startTimeUnixNano": str(start_ns),
                                            "timeUnixNano": str(time_ns),
                                            "count": str(count),
                                            "sum": total,
                                            "bucketCounts": [
                                                str(value) for value in bucket_counts
                                            ],
                                            "explicitBounds": [10, 20],
                                        }
                                    ],
                                },
                            }
                        ],
                    }
                ],
            }
        ]
    }
    return json.dumps(payload).encode("utf-8")


class CollectorMetricsIntegrationTests(unittest.TestCase):
    container_name: str
    input_dir: Path
    otlp_port: int
    metrics_port: int
    temp_dir: tempfile.TemporaryDirectory[str]

    @classmethod
    def setUpClass(cls) -> None:
        if shutil.which("docker") is None:
            raise unittest.SkipTest("Docker is unavailable")
        probe = run_docker("info", check=False)
        if probe.returncode != 0:
            raise unittest.SkipTest("Docker daemon is unavailable")

        cls.temp_dir = tempfile.TemporaryDirectory(prefix="cc-metrics-collector-")
        cls.input_dir = Path(cls.temp_dir.name) / "input"
        cls.input_dir.mkdir()
        (cls.input_dir / "codex-commit-events.jsonl").write_text("", encoding="utf-8")
        cls.container_name = f"cc-metrics-test-{time.time_ns()}"
        result = run_docker(
            "run",
            "-d",
            "--name",
            cls.container_name,
            "-e",
            "HOST_ENV=test",
            "-p",
            "127.0.0.1::4318",
            "-p",
            "127.0.0.1::8889",
            "-v",
            f"{COLLECTOR}:/etc/otel-collector-config.yaml:ro",
            "-v",
            f"{cls.input_dir}:/var/lib/cc-metrics-input:ro",
            collector_image(),
            "--config=/etc/otel-collector-config.yaml",
        )
        if not result.stdout.strip():
            raise RuntimeError("Docker did not return a collector container ID")
        try:
            cls.otlp_port = cls._published_port(4318)
            cls.metrics_port = cls._published_port(8889)
            cls._wait_until_ready()
        except Exception:
            cls._cleanup()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls._cleanup()

    @classmethod
    def _cleanup(cls) -> None:
        if hasattr(cls, "container_name"):
            run_docker("rm", "-f", cls.container_name, check=False)
        if hasattr(cls, "temp_dir"):
            cls.temp_dir.cleanup()

    @classmethod
    def _published_port(cls, container_port: int) -> int:
        result = run_docker("port", cls.container_name, f"{container_port}/tcp")
        mapping = result.stdout.strip().splitlines()[0]
        return int(mapping.rsplit(":", 1)[1])

    @classmethod
    def _wait_until_ready(cls) -> None:
        url = f"http://127.0.0.1:{cls.metrics_port}/metrics"
        last_error: Exception | None = None
        # 20s, not 5s: a cold container image start routinely exceeds a 5s budget,
        # and HTTPException covers RemoteDisconnected - socket open, not yet serving.
        for _ in range(200):
            try:
                with urllib.request.urlopen(url, timeout=1) as response:
                    if response.status == 200:
                        return
            except (OSError, urllib.error.URLError, http.client.HTTPException) as exc:
                last_error = exc
            time.sleep(0.1)
        raise RuntimeError(
            "collector metrics endpoint did not become ready"
        ) from last_error

    def _send(self, payload: bytes) -> None:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.otlp_port}/v1/metrics",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)

    def _scrape(self) -> str:
        url = f"http://127.0.0.1:{self.metrics_port}/metrics"
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.read().decode("utf-8")

    def test_five_commit_log_events_export_cumulative_five(self) -> None:
        audit_file = self.input_dir / "codex-commit-events.jsonl"
        # Readiness covers exporter startup; allow file_log one discovery cycle
        # so start_at=end has established its initial offset before appending.
        time.sleep(0.5)
        for expected in range(1, 6):
            event = json.dumps({"event": "commit", "sequence": expected})
            with audit_file.open("a", encoding="utf-8") as stream:
                stream.write(event + "\n")

            value: float | None = None
            for _ in range(80):
                scrape = self._scrape()
                lines = [
                    line
                    for line in scrape.splitlines()
                    if line.startswith(
                        (
                            "codex_git_commit_count_total{",
                            "codex_git_commit_count_total ",
                        )
                    )
                ]
                if lines:
                    value = float(lines[0].rsplit(" ", 1)[1])
                    if value == expected:
                        break
                time.sleep(0.1)
            self.assertEqual(value, expected)

    def test_two_privacy_normalized_producers_preserve_exact_total(self) -> None:
        secrets = {
            "resource-b",
            "session-b",
            "producer-b@example.invalid",
            "account-b",
            "resource-a",
            "session-a",
            "producer-a@example.invalid",
            "account-a",
            "scope-a-secret",
            "scope-b-secret",
        }
        self._send(
            otlp_delta_payload(
                resource_id="resource-b",
                session_id="session-b",
                email="producer-b@example.invalid",
                account_id="account-b",
                scope_name="scope-b-secret",
                start_ns=3_000_000_000,
                time_ns=4_000_000_000,
                value=20,
            )
        )
        self._send(
            otlp_delta_payload(
                resource_id="resource-a",
                session_id="session-a",
                email="producer-a@example.invalid",
                account_id="account-a",
                scope_name="scope-a-secret",
                start_ns=1_000_000_000,
                time_ns=5_000_000_000,
                value=40,
            )
        )

        scrape = ""
        metric_lines: list[str] = []
        for _ in range(50):
            scrape = self._scrape()
            metric_lines = [
                line
                for line in scrape.splitlines()
                if line.startswith(f"{METRIC_NAME}{{")
            ]
            if len(metric_lines) == 1:
                break
            time.sleep(0.1)

        self.assertEqual(len(metric_lines), 1)
        for secret in secrets:
            self.assertNotIn(secret, scrape)
        self.assertNotRegex(
            scrape,
            r'[{,](service_instance_id|session_id|user_account_uuid|user_email)="',
        )
        self.assertNotIn("producer_resource_id=", scrape)
        self.assertNotIn("producer_session_id=", scrape)
        self.assertIn('agent_name="official-agent"', scrape)
        for rejected in (
            "raw-agent-private",
            "raw-cost-private",
            "raw-tier-private",
            "raw-source-private",
        ):
            self.assertNotIn(rejected, scrape)

        self._send(
            otlp_delta_payload(
                resource_id="resource-b",
                session_id="session-b",
                email="producer-b@example.invalid",
                account_id="account-b",
                scope_name="scope-b-secret",
                start_ns=4_000_000_000,
                time_ns=7_000_000_000,
                value=5,
            )
        )
        self._send(
            otlp_delta_payload(
                resource_id="resource-a",
                session_id="session-a",
                email="producer-a@example.invalid",
                account_id="account-a",
                scope_name="scope-a-secret",
                start_ns=5_000_000_000,
                time_ns=6_000_000_000,
                value=10,
            )
        )
        payloads = [
            otlp_delta_payload(
                resource_id=f"resource-concurrent-{index}",
                session_id=f"session-concurrent-{index}",
                email=f"producer-{index}@example.invalid",
                account_id=f"account-concurrent-{index}",
                scope_name=f"scope-concurrent-{index}",
                start_ns=10_000_000_000 - index,
                time_ns=20_000_000_000 - index,
                value=1,
            )
            for index in range(50)
        ]
        with ThreadPoolExecutor(max_workers=10) as executor:
            list(executor.map(self._send, payloads))

        # Exact emitted total: 20 + 40 + 5 + 10 + fifty one-token deltas.
        for _ in range(50):
            scrape = self._scrape()
            metric_lines = [
                line
                for line in scrape.splitlines()
                if line.startswith(f"{METRIC_NAME}{{")
            ]
            if metric_lines and float(metric_lines[0].rsplit(" ", 1)[1]) == 125:
                break
            time.sleep(0.1)
        self.assertEqual(float(metric_lines[0].rsplit(" ", 1)[1]), 125)
        self.assertNotIn("scope-concurrent-", scrape)

    def test_cumulative_supported_metric_is_rejected_before_compaction(self) -> None:
        rejected_model = "cumulative-must-not-export"
        self._send(
            otlp_delta_payload(
                resource_id="cumulative-resource",
                session_id="cumulative-session",
                email="cumulative@example.invalid",
                account_id="cumulative-account",
                metric_name="claude_code.commit.count",
                model=rejected_model,
                temporality=2,
                start_ns=1_000_000_000,
                time_ns=2_000_000_000,
                value=100,
            )
        )
        time.sleep(0.5)
        self.assertNotIn(rejected_model, self._scrape())

    def test_unknown_delta_metric_is_rejected_by_contract(self) -> None:
        rejected_model = "unknown-family-must-not-export"
        self._send(
            otlp_delta_payload(
                resource_id="unknown-resource",
                session_id="unknown-session",
                email="unknown@example.invalid",
                account_id="unknown-account",
                metric_name="unknown.metric",
                model=rejected_model,
                start_ns=1_000_000_000,
                time_ns=2_000_000_000,
                value=100,
            )
        )
        time.sleep(0.5)
        self.assertNotIn(rejected_model, self._scrape())

    def test_privacy_normalized_histograms_preserve_exact_total(self) -> None:
        payloads = [
            otlp_delta_histogram_payload(
                resource_id="hist-resource-a",
                session_id="hist-session-a",
                scope_name="hist-scope-a",
                start_ns=3_000_000_000,
                time_ns=4_000_000_000,
                count=2,
                total=30,
                bucket_counts=[0, 1, 1],
            ),
            otlp_delta_histogram_payload(
                resource_id="hist-resource-b",
                session_id="hist-session-b",
                scope_name="hist-scope-b",
                start_ns=1_000_000_000,
                time_ns=5_000_000_000,
                count=3,
                total=90,
                bucket_counts=[0, 0, 3],
            ),
        ]
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(self._send, payloads))

        metric_lines: list[str] = []
        bucket_values: dict[str, float] = {}
        for _ in range(50):
            scrape = self._scrape()
            metric_lines = [
                line
                for line in scrape.splitlines()
                if line.startswith(
                    (
                        "codex_turn_token_usage_sum{",
                        "codex_turn_token_usage_count{",
                    )
                )
            ]
            values = {
                line.split("{", 1)[0]: float(line.rsplit(" ", 1)[1])
                for line in metric_lines
            }
            bucket_values = {}
            for line in scrape.splitlines():
                if not line.startswith("codex_turn_token_usage_bucket{"):
                    continue
                boundary = re.search(r'(?:^|,)le="([^"]+)"', line.split("{", 1)[1])
                if boundary is not None:
                    bucket_values[boundary.group(1)] = float(line.rsplit(" ", 1)[1])
            if values == {
                "codex_turn_token_usage_sum": 120,
                "codex_turn_token_usage_count": 5,
            } and bucket_values == {"10": 0, "20": 1, "+Inf": 5}:
                break
            time.sleep(0.1)

        self.assertEqual(
            values,
            {
                "codex_turn_token_usage_sum": 120,
                "codex_turn_token_usage_count": 5,
            },
        )
        self.assertEqual(bucket_values, {"10": 0, "20": 1, "+Inf": 5})
        for secret in (
            "hist-resource-a",
            "hist-resource-b",
            "hist-session-a",
            "hist-session-b",
            "hist-scope-a",
            "hist-scope-b",
        ):
            self.assertNotIn(secret, scrape)

    def test_claude_delta_producers_preserve_exact_total(self) -> None:
        payloads = [
            otlp_delta_payload(
                resource_id="claude-resource-a",
                session_id="claude-session-a",
                email="claude-a@example.invalid",
                account_id="claude-account-a",
                scope_name="claude-scope-a",
                metric_name="claude_code.session.count",
                start_ns=3_000_000_000,
                time_ns=4_000_000_000,
                value=20,
            ),
            otlp_delta_payload(
                resource_id="claude-resource-b",
                session_id="claude-session-b",
                email="claude-b@example.invalid",
                account_id="claude-account-b",
                scope_name="claude-scope-b",
                metric_name="claude_code.session.count",
                start_ns=1_000_000_000,
                time_ns=5_000_000_000,
                value=40,
            ),
        ]
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(self._send, payloads))

        metric_lines: list[str] = []
        for _ in range(50):
            scrape = self._scrape()
            metric_lines = [
                line
                for line in scrape.splitlines()
                if line.startswith("claude_code_session_count_total{")
            ]
            if (
                len(metric_lines) == 1
                and float(metric_lines[0].rsplit(" ", 1)[1]) == 60
            ):
                break
            time.sleep(0.1)

        self.assertEqual(len(metric_lines), 1)
        self.assertEqual(float(metric_lines[0].rsplit(" ", 1)[1]), 60)
        for secret in (
            "claude-resource-a",
            "claude-resource-b",
            "claude-session-a",
            "claude-session-b",
            "claude-a@example.invalid",
            "claude-b@example.invalid",
            "claude-account-a",
            "claude-account-b",
            "claude-scope-a",
            "claude-scope-b",
        ):
            self.assertNotIn(secret, scrape)


if __name__ == "__main__":
    unittest.main()
