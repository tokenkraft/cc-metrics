from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import claude_transcript_exporter as cte  # noqa: E402
from claude_transcript_exporter import (  # noqa: E402
    TranscriptScanner,
    discover_project_roots,
    discover_transcript_files,
    escape_label_value,
    load_state,
    parse_utc,
    save_state,
)


def usage(
    inp: int = 0, out: int = 0, cache_read: int = 0, cache_creation: int = 0
) -> dict:
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_creation,
    }


def assistant_line(
    session_id: str,
    message_id: str,
    timestamp: str,
    model: str = "claude-opus-5",
    effort: str | None = "high",
    usage_dict: dict | None = None,
) -> str:
    """One transcript `assistant` record, minified exactly like the real
    corpus (no whitespace around separators) so it exercises the same
    ASSISTANT_MARKER fast path production code takes."""
    record: dict = {
        "type": "assistant",
        "timestamp": timestamp,
        "sessionId": session_id,
        "message": {
            "id": message_id,
            "model": model,
            "usage": usage_dict if usage_dict is not None else usage(),
        },
    }
    if effort is not None:
        record["effort"] = effort
    return json.dumps(record, separators=(",", ":"))


def user_line(session_id: str, timestamp: str = "2026-08-01T00:00:00.000Z") -> str:
    """A non-assistant record — most real transcript files start with one of
    these, not an assistant record."""
    return json.dumps(
        {
            "type": "user",
            "timestamp": timestamp,
            "sessionId": session_id,
            "message": {"role": "user", "content": "hello"},
        },
        separators=(",", ":"),
    )


class TranscriptFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.projects = self.home / "projects"
        self.projects.mkdir()
        self.addCleanup(self._tmp.cleanup)

    def write(self, rel_path: str, lines: list[str]) -> Path:
        path = self.projects / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def scan(self, state_path: Path | None = None) -> TranscriptScanner:
        scanner = TranscriptScanner(self.home, state_path=state_path)
        scanner.scan()
        return scanner

    def token_total(
        self, scanner: TranscriptScanner, token_type: str | None = None
    ) -> int:
        if token_type is None:
            return sum(scanner.tokens.values())
        return sum(v for (_, _, t), v in scanner.tokens.items() if t == token_type)


# ---------------------------------------------------------------------------
# Trap 1: recursive glob is mandatory — a depth-2 glob misses subagents/.
# ---------------------------------------------------------------------------
class RecursiveDiscoveryTests(TranscriptFixture):
    def test_subagents_subtree_is_discovered(self) -> None:
        """The measured regression: `projects/*/*.jsonl` (depth-2) is blind
        to `projects/*/subagents/*.jsonl`, which carried a large share of
        daily transcript volume on a real corpus. A non-recursive discovery
        function would find only 1 file here, not 2."""
        self.write(
            "proj-a/session1.jsonl",
            [
                assistant_line(
                    "session1",
                    "m1",
                    "2026-08-01T00:00:00.000Z",
                    usage_dict=usage(out=5),
                )
            ],
        )
        self.write(
            "proj-a/subagents/agent1.jsonl",
            [
                assistant_line(
                    "session1",
                    "m2",
                    "2026-08-01T00:00:01.000Z",
                    usage_dict=usage(out=7),
                )
            ],
        )
        found = discover_transcript_files(self.projects)
        rels = {p.relative_to(self.projects) for p in found}
        self.assertEqual(
            rels,
            {Path("proj-a/session1.jsonl"), Path("proj-a/subagents/agent1.jsonl")},
        )

    def test_subagent_tokens_are_counted_in_the_totals(self) -> None:
        """Discovering the file is not enough on its own — its tokens must
        reach the exposed counters too."""
        self.write(
            "proj-a/session1.jsonl",
            [
                assistant_line(
                    "session1",
                    "m1",
                    "2026-08-01T00:00:00.000Z",
                    usage_dict=usage(out=5),
                )
            ],
        )
        self.write(
            "proj-a/subagents/agent1.jsonl",
            [
                assistant_line(
                    "session1",
                    "m2",
                    "2026-08-01T00:00:01.000Z",
                    usage_dict=usage(out=7),
                )
            ],
        )
        scanner = self.scan()
        self.assertEqual(self.token_total(scanner, "output"), 12)


# ---------------------------------------------------------------------------
# Trap 2: dedup by message.id keeping MAX output_tokens, not first / sum.
# ---------------------------------------------------------------------------
class DedupTests(TranscriptFixture):
    def test_dedup_keeps_max_output_tokens_not_first_copy(self) -> None:
        """Streaming copies of one message.id repeat input/cache fields
        unchanged but hold a placeholder output_tokens until the final copy
        (measured: median placeholder 3, 91.8% in 1-6). Keeping the first
        copy understates output by double digits of percent; summing all
        copies overstates every field."""
        mid = "msg_dup1"
        lines = [
            assistant_line(
                "s1", mid, "2026-08-01T00:00:00.000Z", usage_dict=usage(inp=10, out=3)
            ),
            assistant_line(
                "s1", mid, "2026-08-01T00:00:01.000Z", usage_dict=usage(inp=10, out=3)
            ),
            assistant_line(
                "s1", mid, "2026-08-01T00:00:02.000Z", usage_dict=usage(inp=10, out=344)
            ),
        ]
        self.write("proj/s1.jsonl", lines)
        scanner = self.scan()
        self.assertEqual(self.token_total(scanner, "output"), 344)
        self.assertEqual(
            self.token_total(scanner, "input"),
            10,
            "input must not be summed across duplicate copies",
        )

    def test_duplicate_records_counter_reflects_excluded_copies(self) -> None:
        mid = "msg_dup2"
        lines = [
            assistant_line(
                "s1", mid, "2026-08-01T00:00:00.000Z", usage_dict=usage(out=1)
            ),
            assistant_line(
                "s1", mid, "2026-08-01T00:00:01.000Z", usage_dict=usage(out=1)
            ),
            assistant_line(
                "s1", mid, "2026-08-01T00:00:02.000Z", usage_dict=usage(out=9)
            ),
        ]
        self.write("proj/s1.jsonl", lines)
        scanner = self.scan()
        self.assertEqual(scanner.duplicate_records, 2)

    def test_max_wins_regardless_of_file_order(self) -> None:
        """The final copy is usually last, but the rule is MAX, not LAST —
        this must hold even if a copy carrying a smaller output arrives
        after the true value (e.g. across two files touched in an
        unexpected discovery order)."""
        mid = "msg_dup3"
        self.write(
            "proj/s1.jsonl",
            [
                assistant_line(
                    "s1",
                    mid,
                    "2026-08-01T00:00:00.000Z",
                    usage_dict=usage(inp=10, out=344),
                )
            ],
        )
        self.write(
            "proj/subagents/a.jsonl",
            [
                assistant_line(
                    "s1",
                    mid,
                    "2026-08-01T00:00:01.000Z",
                    usage_dict=usage(inp=10, out=3),
                )
            ],
        )
        scanner = self.scan()
        self.assertEqual(self.token_total(scanner, "output"), 344)


# ---------------------------------------------------------------------------
# Trap 3: bound by the record's timestamp field, never file mtime.
# ---------------------------------------------------------------------------
class FreshnessTests(TranscriptFixture):
    def test_freshness_uses_record_timestamp_not_file_mtime(self) -> None:
        """File mtime measurably inflates freshness (~10% on the real
        corpus) — e.g. a backup tool or filesystem reindex touches mtime
        without the transcript content changing."""
        path = self.write(
            "proj/s1.jsonl",
            [
                assistant_line(
                    "s1", "m1", "2026-01-15T12:00:00.000Z", usage_dict=usage(out=1)
                )
            ],
        )
        far_future = time.mktime((2026, 9, 1, 0, 0, 0, 0, 0, -1))
        os.utime(path, (far_future, far_future))
        scanner = self.scan()
        expected = parse_utc("2026-01-15T12:00:00.000Z").timestamp()
        self.assertAlmostEqual(scanner.last_write_unix, expected, delta=1.0)
        self.assertLess(
            scanner.last_write_unix,
            far_future - 1_000_000,
            "freshness gauge picked up the touched mtime, not the record timestamp",
        )

    def test_freshness_tracks_the_latest_record_across_files(self) -> None:
        self.write(
            "proj/s1.jsonl",
            [
                assistant_line(
                    "s1", "m1", "2026-01-01T00:00:00.000Z", usage_dict=usage(out=1)
                )
            ],
        )
        self.write(
            "proj/s2.jsonl",
            [
                assistant_line(
                    "s2", "m2", "2026-06-01T00:00:00.000Z", usage_dict=usage(out=1)
                )
            ],
        )
        scanner = self.scan()
        expected = parse_utc("2026-06-01T00:00:00.000Z").timestamp()
        self.assertAlmostEqual(scanner.last_write_unix, expected, delta=1.0)


# ---------------------------------------------------------------------------
# Trap 4: drop <synthetic> records (all-zero error placeholders).
# ---------------------------------------------------------------------------
class SyntheticTests(TranscriptFixture):
    def test_synthetic_placeholder_is_dropped(self) -> None:
        """Real `<synthetic>` records happen to carry all-zero usage, which
        would mask a broken filter (a zero contributes nothing to the sums
        either way — `if value > 0` alone would hide the regression). Usage
        here is deliberately non-zero so the test exercises the model-based
        filter itself, not the accidental zero-value cover."""
        lines = [
            assistant_line(
                "s1",
                "m1",
                "2026-08-01T00:00:00.000Z",
                model="<synthetic>",
                usage_dict=usage(inp=999, out=999),
            ),
            assistant_line(
                "s1", "m2", "2026-08-01T00:00:01.000Z", usage_dict=usage(inp=5, out=9)
            ),
        ]
        self.write("proj/s1.jsonl", lines)
        scanner = self.scan()
        models = {model for (model, _, _) in scanner.tokens}
        self.assertNotIn("<synthetic>", models)
        self.assertEqual(
            self.token_total(scanner), 14, "synthetic usage leaked into the totals"
        )

    def test_file_with_only_synthetic_records_contributes_no_tokens(self) -> None:
        self.write(
            "proj/s1.jsonl",
            [
                assistant_line(
                    "s1",
                    "m1",
                    "2026-08-01T00:00:00.000Z",
                    model="<synthetic>",
                    usage_dict=usage(inp=999, out=999),
                )
            ],
        )
        scanner = self.scan()
        self.assertEqual(len(scanner.tokens), 0)


# ---------------------------------------------------------------------------
# Trap 5: ~/.claude-profiles/*/projects are symlinks to ~/.claude/projects —
# must not double-count.
# ---------------------------------------------------------------------------
class SymlinkDedupTests(TranscriptFixture):
    def test_symlinked_alias_root_is_not_double_counted(self) -> None:
        self.write(
            "proj/s1.jsonl",
            [
                assistant_line(
                    "s1", "m1", "2026-08-01T00:00:00.000Z", usage_dict=usage(out=5)
                )
            ],
        )
        alias = self.home / "profile-projects"
        alias.symlink_to(self.projects)
        found = discover_transcript_files(self.projects, alias)
        self.assertEqual(len(found), 1)

    def test_symlinked_root_alone_still_finds_files(self) -> None:
        """The dedup guard must not come at the cost of silently returning
        nothing when the ONLY root given is itself a symlink (the failure
        mode `find` has without -L)."""
        self.write(
            "proj/s1.jsonl",
            [
                assistant_line(
                    "s1", "m1", "2026-08-01T00:00:00.000Z", usage_dict=usage(out=5)
                )
            ],
        )
        alias = self.home / "profile-projects"
        alias.symlink_to(self.projects)
        found = discover_transcript_files(alias)
        self.assertEqual(len(found), 1)

    def test_dedup_order_independent(self) -> None:
        self.write(
            "proj/s1.jsonl",
            [
                assistant_line(
                    "s1", "m1", "2026-08-01T00:00:00.000Z", usage_dict=usage(out=5)
                )
            ],
        )
        alias = self.home / "profile-projects"
        alias.symlink_to(self.projects)
        found = discover_transcript_files(alias, self.projects)
        self.assertEqual(len(found), 1)


# ---------------------------------------------------------------------------
# Session counting — distinct sessionId, subagent files share the parent's.
# ---------------------------------------------------------------------------
class SessionCountTests(TranscriptFixture):
    def test_subagent_file_does_not_inflate_session_count(self) -> None:
        self.write(
            "proj/s1.jsonl",
            [
                user_line("s1"),
                assistant_line(
                    "s1", "m1", "2026-08-01T00:00:01.000Z", usage_dict=usage(out=1)
                ),
            ],
        )
        self.write(
            "proj/subagents/agent1.jsonl",
            [
                assistant_line(
                    "s1", "m2", "2026-08-01T00:00:02.000Z", usage_dict=usage(out=1)
                )
            ],
        )
        self.write(
            "proj2/s2.jsonl",
            [
                assistant_line(
                    "s2", "m3", "2026-08-01T00:00:00.000Z", usage_dict=usage(out=1)
                )
            ],
        )
        scanner = self.scan()
        self.assertEqual(scanner.session_count, 2)

    def test_session_id_read_from_a_non_assistant_first_line(self) -> None:
        """Most real files start with a user/queue-operation record, not an
        assistant one — session id must not depend on line 1 being
        assistant-typed."""
        self.write(
            "proj/s1.jsonl",
            [
                user_line("s1"),
                assistant_line(
                    "s1", "m1", "2026-08-01T00:00:01.000Z", usage_dict=usage(out=1)
                ),
            ],
        )
        scanner = self.scan()
        self.assertEqual(scanner.session_count, 1)


# ---------------------------------------------------------------------------
# Incremental caching (performance design).
# ---------------------------------------------------------------------------
class IncrementalCacheTests(TranscriptFixture):
    def test_unchanged_file_is_not_reparsed_on_second_scan(self) -> None:
        path = self.write(
            "proj/s1.jsonl",
            [
                assistant_line(
                    "s1", "m1", "2026-08-01T00:00:00.000Z", usage_dict=usage(out=1)
                )
            ],
        )
        scanner = TranscriptScanner(self.home)
        scanner.scan()
        first_tally = scanner._cache[path]
        scanner.scan()
        second_tally = scanner._cache[path]
        self.assertIs(
            first_tally, second_tally, "unchanged file should reuse its cached parse"
        )

    def test_appended_file_is_reparsed_and_new_records_counted(self) -> None:
        path = self.write(
            "proj/s1.jsonl",
            [
                assistant_line(
                    "s1", "m1", "2026-08-01T00:00:00.000Z", usage_dict=usage(out=1)
                )
            ],
        )
        scanner = TranscriptScanner(self.home)
        scanner.scan()
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(
                assistant_line(
                    "s1", "m2", "2026-08-01T00:00:01.000Z", usage_dict=usage(out=2)
                )
                + "\n"
            )
        scanner.scan()
        self.assertEqual(self.token_total(scanner, "output"), 3)


# ---------------------------------------------------------------------------
# Corpus-shrunk guard (retention-sensitive corpus; state survives restart).
# ---------------------------------------------------------------------------
class CorpusShrunkTests(TranscriptFixture):
    def test_shrink_is_flagged_and_survives_a_restart(self) -> None:
        state_path = self.home / "state.json"
        path = self.write(
            "proj/s1.jsonl",
            [
                assistant_line(
                    "s1", "m1", "2026-08-01T00:00:00.000Z", usage_dict=usage(out=100)
                )
            ],
        )
        scanner = TranscriptScanner(self.home, state_path=state_path)
        scanner.scan()
        self.assertEqual(scanner.corpus_shrunk, 0)
        path.unlink()  # simulates Claude Code's own retention pruning
        scanner.scan()
        self.assertEqual(scanner.corpus_shrunk, 1)
        restarted = TranscriptScanner(self.home, state_path=state_path)
        self.assertEqual(
            restarted.corpus_shrunk,
            1,
            "shrink flag must be readable from persisted state before any scan runs",
        )

    def test_no_shrink_when_corpus_only_grows(self) -> None:
        state_path = self.home / "state.json"
        self.write(
            "proj/s1.jsonl",
            [
                assistant_line(
                    "s1", "m1", "2026-08-01T00:00:00.000Z", usage_dict=usage(out=10)
                )
            ],
        )
        scanner = TranscriptScanner(self.home, state_path=state_path)
        scanner.scan()
        self.write(
            "proj/s2.jsonl",
            [
                assistant_line(
                    "s2", "m2", "2026-08-01T00:00:00.000Z", usage_dict=usage(out=20)
                )
            ],
        )
        scanner.scan()
        self.assertEqual(scanner.corpus_shrunk, 0)


class StateRoundTripTests(unittest.TestCase):
    def test_save_then_load_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            from collections import Counter

            high_water = Counter({("claude-opus-5", "high", "output"): 42})
            save_state(state_path, high_water, 7, 1)
            loaded_water, loaded_sessions, loaded_shrunk = load_state(state_path)
            self.assertEqual(loaded_water, high_water)
            self.assertEqual(loaded_sessions, 7)
            self.assertEqual(loaded_shrunk, 1)

    def test_missing_state_file_loads_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            water, sessions, shrunk = load_state(Path(tmp) / "missing.json")
            self.assertEqual(water, {})
            self.assertEqual(sessions, 0)
            self.assertEqual(shrunk, 0)

    def test_corrupt_state_file_loads_empty_not_raise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state_path.write_text("not json", encoding="utf-8")
            water, sessions, shrunk = load_state(state_path)
            self.assertEqual(water, {})
            self.assertEqual(sessions, 0)
            self.assertEqual(shrunk, 0)


# ---------------------------------------------------------------------------
# Parse robustness.
# ---------------------------------------------------------------------------
class ParseRobustnessTests(TranscriptFixture):
    def test_malformed_line_counts_as_parse_error_and_is_skipped(self) -> None:
        good = assistant_line(
            "s1", "m1", "2026-08-01T00:00:00.000Z", usage_dict=usage(out=1)
        )
        path = self.projects / "proj" / "s1.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(
            good + "\n" + '{"type":"assistant", not valid json\n', encoding="utf-8"
        )
        scanner = self.scan()
        self.assertEqual(scanner.parse_errors, 1)
        self.assertEqual(self.token_total(scanner, "output"), 1)

    def test_missing_usage_counts_as_parse_error(self) -> None:
        record = json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-08-01T00:00:00.000Z",
                "sessionId": "s1",
                "message": {"id": "m1", "model": "claude-opus-5"},
            },
            separators=(",", ":"),
        )
        self.write("proj/s1.jsonl", [record])
        scanner = self.scan()
        self.assertEqual(scanner.parse_errors, 1)
        self.assertEqual(len(scanner.tokens), 0)


# ---------------------------------------------------------------------------
# Exposition text.
# ---------------------------------------------------------------------------
class ExpositionTests(TranscriptFixture):
    def test_exposition_includes_expected_series_and_metadata(self) -> None:
        self.write(
            "proj/s1.jsonl",
            [
                assistant_line(
                    "s1",
                    "m1",
                    "2026-08-01T00:00:00.000Z",
                    model="claude-opus-5",
                    effort="high",
                    usage_dict=usage(inp=10, out=5, cache_read=2, cache_creation=1),
                )
            ],
        )
        scanner = self.scan()
        text = scanner.exposition("test-env")
        self.assertIn(
            'claude_transcript_token_usage_total{env="test-env",effort="high",'
            'model="claude-opus-5",type="input"} 10',
            text,
        )
        self.assertIn('type="output"} 5', text)
        self.assertIn('type="cacheRead"} 2', text)
        self.assertIn('type="cacheCreation"} 1', text)
        self.assertIn('claude_transcript_session_count_total{env="test-env"} 1', text)
        self.assertIn("claude_transcript_scan_ok 1", text)
        self.assertIn("# TYPE claude_transcript_token_usage_total counter", text)
        self.assertIn(
            "# TYPE claude_transcript_last_write_timestamp_seconds gauge", text
        )
        self.assertIn("claude_transcript_corpus_shrunk 0", text)
        self.assertIn("claude_transcript_files_scanned 1", text)

    def test_empty_corpus_still_serves_valid_exposition(self) -> None:
        scanner = self.scan()
        text = scanner.exposition("test-env")
        self.assertIn("claude_transcript_scan_ok 1", text)
        self.assertIn("claude_transcript_files_scanned 0", text)


# ---------------------------------------------------------------------------
# Small helpers.
# ---------------------------------------------------------------------------
class HelperTests(unittest.TestCase):
    def test_parse_utc_accepts_z_suffix(self) -> None:
        parsed = parse_utc("2026-08-01T00:00:05.000Z")
        self.assertEqual(
            parsed.timestamp(), parse_utc("2026-08-01T00:00:05+00:00").timestamp()
        )

    def test_escape_label_value_handles_quotes_and_backslashes(self) -> None:
        self.assertEqual(escape_label_value('a"b\\c\nd'), 'a\\"b\\\\c\\nd')

    def test_default_state_path_is_under_runtime_dir(self) -> None:
        self.assertTrue(
            str(cte.default_state_path()).endswith("claude-transcript-state.json")
        )


if __name__ == "__main__":
    unittest.main()


class ProjectRootDiscoveryTests(unittest.TestCase):
    """Multi-account coverage.

    Every profile exports to the same OTLP endpoint, so the numerator in the
    capture ratio spans all accounts. A witness reading one profile undercounts
    the denominator, which pushes the ratio UP and hides real loss instead of
    raising a false alarm - the silent direction.

    On this host profiles symlink `projects` back to the canonical directory, so
    single-root and multi-root return identical counts and cannot tell the two
    behaviours apart. These tests build real separate directories, which is what
    a freshly created profile actually looks like.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _transcript(self, root: Path, name: str) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        f = root / name
        f.write_text('{"type":"assistant"}\n')
        return f

    def test_separate_profile_dirs_are_all_scanned(self) -> None:
        canonical = self.home / ".claude" / "projects"
        self._transcript(canonical / "a", "one.jsonl")
        for profile in ("acct-one", "acct-two"):
            root = self.home / ".claude-profiles" / profile / "projects" / "p"
            self._transcript(root, f"{profile}.jsonl")

        roots = discover_project_roots(self.home / ".claude")
        self.assertEqual(len(roots), 3, roots)
        self.assertEqual(len(discover_transcript_files(*roots)), 3)

    def test_symlinked_profile_does_not_double_count(self) -> None:
        canonical = self.home / ".claude" / "projects"
        self._transcript(canonical / "a", "one.jsonl")
        profile = self.home / ".claude-profiles" / "acct"
        profile.mkdir(parents=True)
        (profile / "projects").symlink_to(canonical)

        roots = discover_project_roots(self.home / ".claude")
        self.assertEqual(len(roots), 2, roots)
        self.assertEqual(len(discover_transcript_files(*roots)), 1)

    def test_absent_profiles_dir_is_not_an_error(self) -> None:
        """~/.claude-profiles is a convention, not a guaranteed path."""
        canonical = self.home / ".claude" / "projects"
        self._transcript(canonical, "one.jsonl")
        self.assertEqual(discover_project_roots(self.home / ".claude"), [canonical])

    def test_scanner_actually_counts_a_second_profile(self) -> None:
        """The helper being correct proves nothing if the scanner ignores it.

        Reverting the scanner to a single hardcoded root left every other test
        in this class green, because they all call discover_project_roots
        directly. This one goes through TranscriptScanner.scan, so the wiring
        is what fails.
        """
        claude_home = self.home / ".claude"
        canonical = claude_home / "projects" / "proj"
        canonical.mkdir(parents=True)
        (canonical / "main.jsonl").write_text(
            assistant_line(
                "s1", "m1", "2026-08-01T00:00:00.000Z", usage_dict=usage(out=7)
            )
            + "\n",
            encoding="utf-8",
        )

        other = self.home / ".claude-profiles" / "acct-two" / "projects" / "proj"
        other.mkdir(parents=True)
        (other / "main.jsonl").write_text(
            assistant_line(
                "s2", "m2", "2026-08-01T00:00:01.000Z", usage_dict=usage(out=5)
            )
            + "\n",
            encoding="utf-8",
        )

        scanner = TranscriptScanner(claude_home)
        scanner.scan()

        output = sum(v for (_, _, t), v in scanner.tokens.items() if t == "output")
        self.assertEqual(output, 12, "second profile's tokens must be counted")
        self.assertEqual(scanner.session_count, 2)

    def test_extra_root_covers_layouts_outside_the_convention(self) -> None:
        canonical = self.home / ".claude" / "projects"
        self._transcript(canonical, "one.jsonl")
        elsewhere = self.home / "custom" / "projects"
        self._transcript(elsewhere, "two.jsonl")

        roots = discover_project_roots(self.home / ".claude", (elsewhere,))
        self.assertIn(elsewhere, roots)
        self.assertEqual(len(discover_transcript_files(*roots)), 2)
