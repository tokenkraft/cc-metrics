from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import codex_ledger  # noqa: E402
from codex_ledger import (  # noqa: E402
    discover_session_files,
    fork_roots,
    iter_usage_records,
    resolve_fork_roots,
    record_key,
)


def usage(inp: int, cached: int = 0, out: int = 0, reasoning: int = 0) -> dict:
    return {
        "input_tokens": inp,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": 0,
        "output_tokens": out,
        "reasoning_output_tokens": reasoning,
        "total_tokens": inp + out,
    }


def meta_line(
    session_id: str | None, ident: str, forked_from: str | None = None
) -> str:
    payload: dict = {"id": ident}
    if session_id is not None:
        payload["session_id"] = session_id
    if forked_from is not None:
        payload["forked_from_id"] = forked_from
    return json.dumps(
        {
            "timestamp": "2026-08-01T00:00:00.000Z",
            "type": "session_meta",
            "payload": payload,
        }
    )


def turn_line(model: str | None = "gpt-5.6-sol", effort: str | None = "high") -> str:
    payload: dict = {"turn_id": "t1"}
    if model is not None:
        payload["model"] = model
    if effort is not None:
        payload["effort"] = effort
    return json.dumps(
        {
            "timestamp": "2026-08-01T00:00:01.000Z",
            "type": "turn_context",
            "payload": payload,
        }
    )


def token_line(ts: str, last: dict, total: dict) -> str:
    return json.dumps(
        {
            "timestamp": ts,
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"last_token_usage": last, "total_token_usage": total},
            },
        }
    )


class LedgerFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        (self.home / "sessions").mkdir()
        self.addCleanup(self._tmp.cleanup)

    def write(self, name: str, lines: list[str]) -> Path:
        path = self.home / "sessions" / name
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def records(self, path: Path) -> tuple[list, int]:
        errors = [0]
        return list(iter_usage_records(path, errors)), errors[0]

    def dedup(self) -> list:
        """Every record across the corpus, deduped by key — first-seen copy
        kept. Production callers resolve to the EARLIEST copy instead; that
        rule has its own tests (backfill and ExporterEarliestCopyTests)."""
        seen: set = set()
        kept = []
        errors = [0]
        paths = discover_session_files(self.home)
        roots = fork_roots(paths)
        for path in paths:
            for record in iter_usage_records(path, errors, roots):
                if record.key in seen:
                    continue
                seen.add(record.key)
                kept.append(record)
        return kept


class RecordKeyTests(LedgerFixture):
    def test_replay_restamped_with_a_new_timestamp_still_dedups(self) -> None:
        """The defect this key exists for: a continuation rewrites replayed
        timestamps, so a timestamp-bearing key double-counts the tokens."""
        call = usage(1000, out=50)
        total = usage(1000, out=50)
        self.write(
            "rollout-2026-08-01T00-00-00-aaa.jsonl",
            [
                meta_line("lineage-1", "aaa"),
                turn_line(),
                token_line("2026-08-01T00:00:05.000Z", call, total),
            ],
        )
        self.write(
            "rollout-2026-08-02T00-00-00-bbb.jsonl",
            [
                meta_line("lineage-1", "bbb", forked_from="aaa"),
                # replayed with the fork's own clock, not the original's
                token_line("2026-08-02T09:30:00.000Z", call, total),
                turn_line(),
            ],
        )
        kept = self.dedup()
        self.assertEqual(len(kept), 1, "replay was counted twice")
        self.assertEqual(kept[0].timestamp, "2026-08-01T00:00:05.000Z")

    def test_resume_without_forked_from_id_also_dedups(self) -> None:
        """Resume replays carry no forked_from_id — keying on fork metadata
        alone would miss them."""
        call = usage(700, out=20)
        total = usage(700, out=20)
        self.write(
            "rollout-2026-08-01T00-00-00-aaa.jsonl",
            [
                meta_line("lineage-2", "aaa"),
                turn_line(),
                token_line("2026-08-01T00:00:05.000Z", call, total),
            ],
        )
        self.write(
            "rollout-2026-08-03T00-00-00-ccc.jsonl",
            [
                meta_line("lineage-2", "ccc"),
                turn_line(),
                token_line("2026-08-03T11:00:00.000Z", call, total),
            ],
        )
        self.assertEqual(len(self.dedup()), 1)

    def test_same_cumulative_with_different_call_is_kept(self) -> None:
        """Diverging branches of one lineage can share a cumulative total;
        dropping them on the cumulative alone destroys real usage."""
        total = usage(5000, out=100)
        self.write(
            "rollout-2026-08-01T00-00-00-aaa.jsonl",
            [
                meta_line("lineage-3", "aaa"),
                turn_line(),
                token_line("2026-08-01T00:00:05.000Z", usage(400, out=10), total),
            ],
        )
        self.write(
            "rollout-2026-08-01T01-00-00-bbb.jsonl",
            [
                meta_line("lineage-3", "bbb", forked_from="aaa"),
                turn_line(),
                token_line("2026-08-01T01:00:05.000Z", usage(900, out=30), total),
            ],
        )
        self.assertEqual(len(self.dedup()), 2)

    def test_old_schema_fork_dedups_against_its_parent(self) -> None:
        """Older session_meta carries no session_id. A fork there is linked to
        its parent only by forked_from_id; keying it on its own id counted the
        replayed parent records twice."""
        call, total = usage(1000, out=50), usage(1000, out=50)
        self.write(
            "rollout-2026-08-01T00-00-00-aaa.jsonl",
            [
                meta_line(None, "aaa"),
                turn_line(),
                token_line("2026-08-01T00:00:05.000Z", call, total),
            ],
        )
        self.write(
            "rollout-2026-08-01T00-01-00-bbb.jsonl",
            [
                meta_line(None, "bbb", forked_from="aaa"),
                token_line("2026-08-01T00:01:05.000Z", call, total),
                turn_line(),
            ],
        )
        self.assertEqual(len(self.dedup()), 1)

    def test_nested_old_schema_forks_resolve_to_the_root_lineage(self) -> None:
        """A -> B -> C with no session_id anywhere: C's replay of A's record
        must key on A, not on B, or it escapes the dedup."""
        call, total = usage(1000, out=50), usage(1000, out=50)
        self.write(
            "rollout-2026-08-01T00-00-00-aaa.jsonl",
            [
                meta_line(None, "aaa"),
                turn_line(),
                token_line("2026-08-01T00:00:05.000Z", call, total),
            ],
        )
        self.write(
            "rollout-2026-08-01T00-01-00-bbb.jsonl",
            [meta_line(None, "bbb", forked_from="aaa"), turn_line()],
        )
        self.write(
            "rollout-2026-08-01T00-02-00-ccc.jsonl",
            [
                meta_line(None, "ccc", forked_from="bbb"),
                token_line("2026-08-01T00:02:05.000Z", call, total),
                turn_line(),
            ],
        )
        paths = discover_session_files(self.home)
        self.assertEqual(fork_roots(paths), {"bbb": "aaa", "ccc": "aaa"})
        self.assertEqual(len(self.dedup()), 1)

    def test_fork_cycle_members_share_one_root(self) -> None:
        """A malformed a -> b -> a link cycle must not leave the members on
        different roots, or their replays escape the dedup."""
        roots = resolve_fork_roots([("b", "a"), ("a", "b"), ("c", "b")])
        self.assertEqual(roots, {"a": "a", "b": "a", "c": "a"})

    def test_records_without_a_lineage_are_skipped_not_merged(self) -> None:
        """Two unrelated files with no session_meta and byte-identical usage
        would share the empty lineage and collapse into one record. They are
        parse errors instead."""
        call = usage(100, out=5)
        for name in ("aaa", "bbb"):
            self.write(
                f"rollout-2026-08-01T00-00-00-{name}.jsonl",
                [turn_line(), token_line("2026-08-01T00:00:05.000Z", call, call)],
            )
        errors = [0]
        kept = [
            record
            for path in discover_session_files(self.home)
            for record in iter_usage_records(path, errors)
        ]
        self.assertEqual(kept, [])
        self.assertEqual(errors[0], 2)

    def test_distinct_lineages_never_collide(self) -> None:
        call = usage(100, out=5)
        for name, lineage in (("aaa", "one"), ("bbb", "two")):
            self.write(
                f"rollout-2026-08-01T00-00-00-{name}.jsonl",
                [
                    meta_line(lineage, name),
                    turn_line(),
                    token_line("2026-08-01T00:00:05.000Z", call, call),
                ],
            )
        self.assertEqual(len(self.dedup()), 2)

    def test_key_collapses_on_stamp_but_splits_on_cumulative(self) -> None:
        """Timestamp must not participate; lineage and cumulative must. A key
        that ignores its inputs would collapse all four of these."""
        base = record_key("lineage", usage(500), usage(10))
        self.assertEqual(base, record_key("lineage", usage(500), usage(10)))
        self.assertNotEqual(base, record_key("other", usage(500), usage(10)))
        self.assertNotEqual(base, record_key("lineage", usage(600), usage(10)))
        self.assertNotEqual(base, record_key("lineage", usage(500), usage(20)))


class LineageTests(LedgerFixture):
    def test_session_id_falls_back_to_id(self) -> None:
        """Some session_meta records carry only `id`; without the fallback
        every such file shares the empty lineage and cross-dedups."""
        call = usage(100, out=5)
        self.write(
            "rollout-2026-08-01T00-00-00-aaa.jsonl",
            [
                meta_line(None, "aaa"),
                turn_line(),
                token_line("2026-08-01T00:00:05.000Z", call, call),
            ],
        )
        self.write(
            "rollout-2026-08-01T01-00-00-bbb.jsonl",
            [
                meta_line(None, "bbb"),
                turn_line(),
                token_line("2026-08-01T01:00:05.000Z", call, call),
            ],
        )
        self.assertEqual(len(self.dedup()), 2)

    def test_repeated_session_meta_for_the_same_lineage_keeps_context(self) -> None:
        """Codex re-emits session_meta mid-file for the SAME session. Treating
        that as a new context strands the token_count records written before
        the next turn_context, labelling real usage model="unknown"."""
        path = self.write(
            "rollout-2026-08-01T00-00-00-aaa.jsonl",
            [
                meta_line("lineage-a", "aaa"),
                turn_line(model="gpt-5.5", effort="xhigh"),
                token_line("2026-08-01T00:00:05.000Z", usage(10), usage(10)),
                meta_line("lineage-a", "aaa"),
                token_line("2026-08-01T00:10:05.000Z", usage(20), usage(20)),
            ],
        )
        records, _ = self.records(path)
        self.assertEqual([r.model for r in records], ["gpt-5.5", "gpt-5.5"])
        self.assertEqual([r.effort for r in records], ["xhigh", "xhigh"])

    def test_second_session_meta_starts_a_fresh_context(self) -> None:
        path = self.write(
            "rollout-2026-08-01T00-00-00-aaa.jsonl",
            [
                meta_line("lineage-a", "aaa"),
                turn_line(model="gpt-5.6-sol", effort="high"),
                token_line("2026-08-01T00:00:05.000Z", usage(10), usage(10)),
                meta_line("lineage-b", "bbb"),
                token_line("2026-08-01T00:10:05.000Z", usage(20), usage(20)),
            ],
        )
        records, _ = self.records(path)
        self.assertEqual(records[0].model, "gpt-5.6-sol")
        self.assertEqual(records[0].effort, "high")
        self.assertEqual(
            records[1].model, "unknown", "stale model leaked past session_meta"
        )
        self.assertEqual(records[1].effort, "", "stale effort leaked past session_meta")
        self.assertNotEqual(records[0].key[0], records[1].key[0])


class ContextTrackingTests(LedgerFixture):
    def test_effort_is_labelled_per_turn(self) -> None:
        path = self.write(
            "rollout-2026-08-01T00-00-00-aaa.jsonl",
            [
                meta_line("lineage", "aaa"),
                turn_line(effort="high"),
                token_line("2026-08-01T00:00:05.000Z", usage(10), usage(10)),
                turn_line(effort="low"),
                token_line("2026-08-01T00:00:06.000Z", usage(20), usage(20)),
            ],
        )
        records, _ = self.records(path)
        self.assertEqual([r.effort for r in records], ["high", "low"])

    def test_turn_without_effort_resets_it(self) -> None:
        """Effort is a per-turn setting; carrying the previous turn's value
        into a turn that omits it mislabels that turn."""
        path = self.write(
            "rollout-2026-08-01T00-00-00-aaa.jsonl",
            [
                meta_line("lineage", "aaa"),
                turn_line(effort="high"),
                token_line("2026-08-01T00:00:05.000Z", usage(10), usage(10)),
                turn_line(effort=None),
                token_line("2026-08-01T00:00:06.000Z", usage(20), usage(20)),
            ],
        )
        records, _ = self.records(path)
        self.assertEqual([r.effort for r in records], ["high", ""])

    def test_turn_with_empty_model_keeps_the_previous_model(self) -> None:
        path = self.write(
            "rollout-2026-08-01T00-00-00-aaa.jsonl",
            [
                meta_line("lineage", "aaa"),
                turn_line(model="gpt-5.6-sol"),
                token_line("2026-08-01T00:00:05.000Z", usage(10), usage(10)),
                turn_line(model=""),
                token_line("2026-08-01T00:00:06.000Z", usage(20), usage(20)),
            ],
        )
        records, _ = self.records(path)
        self.assertEqual([r.model for r in records], ["gpt-5.6-sol", "gpt-5.6-sol"])


class ParseRobustnessTests(LedgerFixture):
    def test_truncated_tail_counts_as_a_parse_error_and_is_skipped(self) -> None:
        path = self.home / "sessions" / "rollout-2026-08-01T00-00-00-aaa.jsonl"
        good = token_line("2026-08-01T00:00:05.000Z", usage(10), usage(10))
        path.write_text(
            "\n".join(
                [
                    meta_line("lineage", "aaa"),
                    turn_line(),
                    good,
                    '{"type":"token_count"',
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        records, errors = self.records(path)
        self.assertEqual(len(records), 1)
        self.assertEqual(errors, 1)

    def test_record_without_cumulative_is_not_silently_counted(self) -> None:
        """No cumulative means no replay-stable identity — such a record must
        be reported, not folded in under a fabricated key."""
        line = json.dumps(
            {
                "timestamp": "2026-08-01T00:00:05.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"last_token_usage": usage(10)},
                },
            }
        )
        path = self.write(
            "rollout-2026-08-01T00-00-00-aaa.jsonl",
            [meta_line("lineage", "aaa"), turn_line(), line],
        )
        records, errors = self.records(path)
        self.assertEqual(records, [])
        self.assertEqual(errors, 1)

    def test_session_meta_survives_the_line_marker_fast_path(self) -> None:
        """LINE_MARKERS gates JSON parsing. If session_meta were dropped from
        it the lineage id would never reach a record, and every file would
        dedup against every other under the empty lineage."""
        path = self.write(
            "rollout-2026-08-01T00-00-00-aaa.jsonl",
            [
                meta_line("lineage-xyz", "aaa"),
                turn_line(),
                token_line("2026-08-01T00:00:05.000Z", usage(10), usage(10)),
            ],
        )
        records, _ = self.records(path)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].key[0], "lineage-xyz")


class DiscoveryTests(LedgerFixture):
    def test_active_copy_wins_over_archived_of_the_same_name(self) -> None:
        (self.home / "archived_sessions").mkdir()
        name = "rollout-2026-08-01T00-00-00-aaa.jsonl"
        self.write(name, [meta_line("lineage", "aaa")])
        (self.home / "archived_sessions" / name).write_text("{}\n", encoding="utf-8")
        found = discover_session_files(self.home)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].parent.name, "sessions")

    def test_archived_only_files_are_still_discovered(self) -> None:
        """Discovery that looked at sessions/ alone would silently drop every
        archived session — the bulk of the corpus."""
        (self.home / "archived_sessions").mkdir()
        (
            self.home / "archived_sessions" / "rollout-2026-01-01T00-00-00-zzz.jsonl"
        ).write_text("{}\n", encoding="utf-8")
        self.write("rollout-2026-08-01T00-00-00-aaa.jsonl", [meta_line("l", "aaa")])
        found = discover_session_files(self.home)
        self.assertEqual(len(found), 2)
        self.assertIn("archived_sessions", {p.parent.name for p in found})


class TokenFieldTests(unittest.TestCase):
    def test_total_is_carried_as_its_own_type(self) -> None:
        """`total` is Codex's own figure and is not input + output, so it has
        to travel as a distinct token_type rather than be reconstructed."""
        self.assertEqual(codex_ledger.TOKEN_FIELDS["total_tokens"], "total")
        self.assertEqual(len(set(codex_ledger.TOKEN_FIELDS.values())), 6)


class ExporterExpositionTests(LedgerFixture):
    def scan(self):
        import codex_ledger_exporter

        scanner = codex_ledger_exporter.LedgerScanner(self.home)
        scanner.scan()
        return scanner

    def test_effort_is_exposed_as_a_label(self) -> None:
        self.write(
            "rollout-2026-08-01T00-00-00-aaa.jsonl",
            [
                meta_line("lineage", "aaa"),
                turn_line(effort="xhigh"),
                token_line(
                    "2026-08-01T00:00:05.000Z", usage(100, out=10), usage(100, out=10)
                ),
            ],
        )
        body = self.scan().exposition("test-env")
        self.assertIn(
            'codex_ledger_token_usage_total{env="test-env",effort="xhigh",'
            'model="gpt-5.6-sol",token_type="input"} 100',
            body,
        )
        self.assertIn(
            'codex_ledger_turn_records_total{env="test-env",effort="xhigh",'
            'model="gpt-5.6-sol"} 1',
            body,
        )

    def test_replay_is_not_double_counted_in_the_exposition(self) -> None:
        call, total = usage(1000, out=50), usage(1000, out=50)
        self.write(
            "rollout-2026-08-01T00-00-00-aaa.jsonl",
            [
                meta_line("lineage", "aaa"),
                turn_line(),
                token_line("2026-08-01T00:00:05.000Z", call, total),
            ],
        )
        self.write(
            "rollout-2026-08-02T00-00-00-bbb.jsonl",
            [
                meta_line("lineage", "bbb", forked_from="aaa"),
                token_line("2026-08-02T09:30:00.000Z", call, total),
                turn_line(),
            ],
        )
        scanner = self.scan()
        self.assertEqual(scanner.tokens[("gpt-5.6-sol", "high", "input")], 1000)
        self.assertEqual(scanner.duplicate_records, 1)

    def test_fork_replay_leaves_no_unknown_model_series(self) -> None:
        """The replay region precedes the fork's first turn_context, so it has
        no model; it must dedup away rather than surface as model="unknown"."""
        call, total = usage(1000, out=50), usage(1000, out=50)
        self.write(
            "rollout-2026-08-01T00-00-00-aaa.jsonl",
            [
                meta_line("lineage", "aaa"),
                turn_line(),
                token_line("2026-08-01T00:00:05.000Z", call, total),
            ],
        )
        self.write(
            "rollout-2026-08-02T00-00-00-bbb.jsonl",
            [
                meta_line("lineage", "bbb", forked_from="aaa"),
                token_line("2026-08-02T09:30:00.000Z", call, total),
                turn_line(),
            ],
        )
        body = self.scan().exposition("test-env")
        # positive assertion first: an exposition that emitted nothing at all
        # would satisfy the negative one on its own
        self.assertIn(
            'codex_ledger_token_usage_total{env="test-env",effort="high",'
            'model="gpt-5.6-sol",token_type="total"} 1050',
            body,
        )
        self.assertNotIn('model="unknown"', body)


class ExporterEarliestCopyTests(LedgerFixture):
    """Discovery is active-then-archived. A `codex resume` in sessions/ replays
    its archived parent's records before its own turn_context; first-wins
    labelled that usage model="unknown" and, once the continuation file was
    archived, the label flipped — a per-series decrease the shrink guard
    reads as a deleted corpus."""

    def corpus(self) -> None:
        call, total = usage(1000, out=50), usage(1000, out=50)
        (self.home / "archived_sessions").mkdir(exist_ok=True)
        (
            self.home / "archived_sessions" / "rollout-2026-08-01T00-00-00-aaa.jsonl"
        ).write_text(
            "\n".join(
                [
                    meta_line("lineage", "aaa"),
                    turn_line(),
                    token_line("2026-08-01T00:00:05.000Z", call, total),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        self.write(
            "rollout-2026-09-01T00-00-00-bbb.jsonl",
            [
                meta_line("lineage", "bbb"),
                token_line("2026-09-01T00:00:05.000Z", call, total),
                turn_line(),
            ],
        )

    def test_archived_original_labels_win_over_active_replay(self) -> None:
        import codex_ledger_exporter

        self.corpus()
        scanner = codex_ledger_exporter.LedgerScanner(self.home)
        scanner.scan()
        self.assertEqual(
            dict(scanner.tokens),
            {
                ("gpt-5.6-sol", "high", "input"): 1000,
                ("gpt-5.6-sol", "high", "output"): 50,
                ("gpt-5.6-sol", "high", "total"): 1050,
            },
        )
        self.assertEqual(scanner.duplicate_records, 1)

    def test_archiving_the_continuation_does_not_trip_the_shrink_guard(self) -> None:
        import codex_ledger_exporter

        self.corpus()
        scanner = codex_ledger_exporter.LedgerScanner(
            self.home, state_path=self.home / "state.json"
        )
        scanner.scan()
        before = dict(scanner.tokens)
        (self.home / "sessions" / "rollout-2026-09-01T00-00-00-bbb.jsonl").rename(
            self.home / "archived_sessions" / "rollout-2026-09-01T00-00-00-bbb.jsonl"
        )
        scanner.scan()
        self.assertEqual(dict(scanner.tokens), before)
        self.assertEqual(scanner.corpus_shrunk, 0)


class ShrinkGuardTests(LedgerFixture):
    """The guard exists to catch a corpus that stopped being append-only.
    An in-memory-only baseline missed exactly the case that matters — a
    shrink across a restart or an exporter-version change."""

    def scanner(self, state_path):
        import codex_ledger_exporter

        return codex_ledger_exporter.LedgerScanner(self.home, state_path=state_path)

    def one_record(self, name, lineage, total):
        self.write(
            name,
            [
                meta_line(lineage, lineage),
                turn_line(),
                token_line("2026-08-01T00:00:05.000Z", usage(total), usage(total)),
            ],
        )

    def test_shrink_across_a_restart_is_caught(self) -> None:
        state = self.home / "state.json"
        self.one_record("rollout-2026-08-01T00-00-00-aaa.jsonl", "a", 100)
        self.one_record("rollout-2026-08-01T01-00-00-bbb.jsonl", "b", 100)
        first = self.scanner(state)
        first.scan()
        self.assertEqual(first.corpus_shrunk, 0)
        # corpus stops being append-only while the exporter is down
        (self.home / "sessions" / "rollout-2026-08-01T01-00-00-bbb.jsonl").unlink()
        restarted = self.scanner(state)
        restarted.scan()
        self.assertEqual(restarted.corpus_shrunk, 1, "restart reset the guard's memory")

    def test_growth_across_a_restart_is_not_flagged(self) -> None:
        state = self.home / "state.json"
        self.one_record("rollout-2026-08-01T00-00-00-aaa.jsonl", "a", 100)
        self.scanner(state).scan()
        self.one_record("rollout-2026-08-01T01-00-00-bbb.jsonl", "b", 100)
        restarted = self.scanner(state)
        restarted.scan()
        self.assertEqual(restarted.corpus_shrunk, 0)

    def test_shrink_flag_itself_survives_a_restart(self) -> None:
        state = self.home / "state.json"
        self.one_record("rollout-2026-08-01T00-00-00-aaa.jsonl", "a", 100)
        self.one_record("rollout-2026-08-01T01-00-00-bbb.jsonl", "b", 100)
        self.scanner(state).scan()
        (self.home / "sessions" / "rollout-2026-08-01T01-00-00-bbb.jsonl").unlink()
        self.scanner(state).scan()
        self.assertEqual(self.scanner(state).corpus_shrunk, 1)

    def test_unreadable_state_does_not_stop_the_scan(self) -> None:
        state = self.home / "state.json"
        state.write_text("{not json", encoding="utf-8")
        self.one_record("rollout-2026-08-01T00-00-00-aaa.jsonl", "a", 100)
        scanner = self.scanner(state)
        scanner.scan()
        self.assertEqual(scanner.scan_ok, 1)
        self.assertEqual(scanner.corpus_shrunk, 0)

    def test_no_state_path_keeps_the_guard_in_memory(self) -> None:
        """Without a state path the guard still works within one process — it
        just cannot span a restart — and nothing is persisted."""
        self.one_record("rollout-2026-08-01T00-00-00-aaa.jsonl", "a", 100)
        self.one_record("rollout-2026-08-01T01-00-00-bbb.jsonl", "b", 100)
        scanner = self.scanner(None)
        scanner.scan()
        self.assertEqual(scanner.tokens[("gpt-5.6-sol", "high", "total")], 200)
        self.assertEqual(scanner.corpus_shrunk, 0)
        (self.home / "sessions" / "rollout-2026-08-01T01-00-00-bbb.jsonl").unlink()
        scanner.scan()
        self.assertEqual(scanner.tokens[("gpt-5.6-sol", "high", "total")], 100)
        self.assertEqual(scanner.corpus_shrunk, 1, "in-process shrink missed")
        self.assertEqual(list(self.home.glob("*.json")), [], "state was persisted")


class BackfillTests(LedgerFixture):
    """The backfill splices under the live counter, so it must produce the
    same series the exporter would. It had no tests until 2026-08-25."""

    def collect(self, end="2026-08-25T08:00:00"):
        import backfill_codex_ledger_history as bf

        return bf, bf.collect_hourly(self.home, bf.parse_utc(end))

    def emit(self, raw: bool, end="2026-08-25T08:00:00"):
        """Render through the REAL emission path. Re-implementing the loop
        here would make these tests pass against any change to it."""
        bf, hourly = self.collect(end)
        lines, _, _ = bf.render_samples(hourly, bf.parse_utc(end), 'job="t"', raw)
        return lines

    def test_zero_valued_series_are_not_emitted(self) -> None:
        """The exporter omits a series while its value is 0. Emitting one here
        splices series into history that the live lane never produces."""
        self.write(
            "rollout-2026-08-01T00-00-00-aaa.jsonl",
            [
                meta_line("lineage", "aaa"),
                turn_line(),
                # no cache_write and no reasoning -> those must not appear
                token_line(
                    "2026-08-01T00:00:05.000Z", usage(100, out=10), usage(100, out=10)
                ),
            ],
        )
        for raw, absent in ((True, "cache_write_input"), (False, "cacheCreation")):
            lines = self.emit(raw=raw)
            self.assertTrue(lines, "backfill produced nothing")
            values = [int(ln.rsplit(" ", 2)[-2]) for ln in lines]
            self.assertEqual(
                [v for v in values if v <= 0], [], "zero-valued series emitted"
            )
            self.assertFalse(
                [ln for ln in lines if absent in ln],
                f"emitted an all-zero {absent} series",
            )

    def test_every_positive_token_type_is_emitted(self) -> None:
        """A raw emitter restricted to a subset of TOKEN_FIELDS would still
        satisfy a totals check; assert the full set reaches the output."""
        rich = {
            "input_tokens": 100,
            "cached_input_tokens": 40,
            "cache_write_input_tokens": 7,
            "output_tokens": 20,
            "reasoning_output_tokens": 5,
            "total_tokens": 120,
        }
        self.write(
            "rollout-2026-08-01T00-00-00-aaa.jsonl",
            [
                meta_line("lineage", "aaa"),
                turn_line(),
                token_line("2026-08-01T00:00:05.000Z", rich, rich),
            ],
        )
        types = {
            re.search(r'token_type="([^"]*)"', ln).group(1)
            for ln in self.emit(raw=True)
        }
        self.assertEqual(types, set(codex_ledger.TOKEN_FIELDS.values()))

    def test_unparseable_timestamp_does_not_consume_the_dedup_key(self) -> None:
        """A record skipped for a bad timestamp must not claim its identity —
        the valid replay that follows would then be dropped as a duplicate."""
        call, total = usage(1000, out=50), usage(1000, out=50)
        self.write(
            "rollout-2026-08-01T00-00-00-aaa.jsonl",
            [
                meta_line("lineage", "aaa"),
                turn_line(),
                token_line("not-a-timestamp", call, total),
            ],
        )
        self.write(
            "rollout-2026-08-02T00-00-00-bbb.jsonl",
            [
                meta_line("lineage", "bbb", forked_from="aaa"),
                turn_line(),
                token_line("2026-08-01T00:00:05.000Z", call, total),
            ],
        )
        _, hourly = self.collect()
        billed = sum(
            v
            for c in hourly.values()
            for (_, _, f), v in c.items()
            if f == "total_tokens"
        )
        self.assertEqual(billed, 1050, "valid copy was suppressed by the bad one")

    def test_post_cutoff_replay_does_not_drop_the_in_range_original(self) -> None:
        """Discovery is active-then-archived, not chronological. A replay in
        sessions/ is seen before its original in archived_sessions/; bucketing
        the replay would file the tokens at its restamped time and, past the
        cutoff, discard the original outright."""
        call, total = usage(1000, out=50), usage(1000, out=50)
        (self.home / "archived_sessions").mkdir(exist_ok=True)
        (
            self.home / "archived_sessions" / "rollout-2026-08-01T00-00-00-aaa.jsonl"
        ).write_text(
            "\n".join(
                [
                    meta_line("lineage", "aaa"),
                    turn_line(),
                    token_line("2026-08-01T00:00:05.000Z", call, total),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        self.write(
            "rollout-2026-09-01T00-00-00-bbb.jsonl",
            [
                meta_line("lineage", "bbb", forked_from="aaa"),
                turn_line(),
                token_line("2026-09-01T00:00:05.000Z", call, total),
            ],
        )
        _, hourly = self.collect(end="2026-08-02T00:00:00")
        billed = sum(
            v
            for c in hourly.values()
            for (_, _, f), v in c.items()
            if f == "total_tokens"
        )
        self.assertEqual(billed, 1050, "in-range original was dropped by its replay")

    def test_replay_is_deduped_exactly_once(self) -> None:
        call, total = usage(1000, out=50), usage(1000, out=50)
        self.write(
            "rollout-2026-08-01T00-00-00-aaa.jsonl",
            [
                meta_line("lineage", "aaa"),
                turn_line(),
                token_line("2026-08-01T00:00:05.000Z", call, total),
            ],
        )
        self.write(
            "rollout-2026-08-02T00-00-00-bbb.jsonl",
            [
                meta_line("lineage", "bbb", forked_from="aaa"),
                token_line("2026-08-02T09:30:00.000Z", call, total),
                turn_line(),
            ],
        )
        _, hourly = self.collect()
        billed = sum(
            v
            for c in hourly.values()
            for (_, _, f), v in c.items()
            if f == "total_tokens"
        )
        self.assertEqual(billed, 1050)

    def test_effort_reaches_the_emitted_series(self) -> None:
        """Two different efforts, so a constant label cannot satisfy this."""
        self.write(
            "rollout-2026-08-01T00-00-00-aaa.jsonl",
            [
                meta_line("lineage", "aaa"),
                turn_line(effort="xhigh"),
                token_line(
                    "2026-08-01T00:00:05.000Z", usage(100, out=10), usage(100, out=10)
                ),
                turn_line(effort="low"),
                token_line(
                    "2026-08-01T01:00:05.000Z", usage(200, out=20), usage(300, out=30)
                ),
            ],
        )
        lines = self.emit(raw=True)
        self.assertTrue(lines)
        efforts = {re.search(r'effort="([^"]*)"', ln).group(1) for ln in lines}
        self.assertEqual(efforts, {"xhigh", "low"})


if __name__ == "__main__":
    unittest.main()
