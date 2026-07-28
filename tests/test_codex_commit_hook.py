from __future__ import annotations

import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from types import ModuleType
from unittest.mock import patch


@contextmanager
def quiet():
    """Swallow the installer's receipt lines and argparse errors.

    These tests exercise the installer's reporting and its failure paths, both
    of which write to the console by design. Left unredirected they interleave
    with unittest's own output, so a passing run appears to contain an error
    about the reader's own ``hooks.json``.
    """
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        yield


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = PROJECT_ROOT / "scripts" / "codex_commit_hook.py"
INSTALLER_PATH = PROJECT_ROOT / "scripts" / "install_codex_commit_hook.py"


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


hook = _load_module("codex_commit_hook", HOOK_PATH)
installer = _load_module("install_codex_commit_hook", INSTALLER_PATH)


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


class CodexCommitHookTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.runtime = self.root / "runtime"
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q")
        _git(self.repo, "config", "user.name", "Codex Hook Test")
        _git(self.repo, "config", "user.email", "codex-hook@example.invalid")
        self.environment = patch.dict(
            os.environ, {"CC_METRICS_RUNTIME_DIR": str(self.runtime)}
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def _payload(
        self, event: str, command: str = "git commit -m test"
    ) -> dict[str, object]:
        return {
            "hook_event_name": event,
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "cwd": str(self.repo),
            "session_id": "session-test",
            "tool_use_id": "tool-test",
            "model": "gpt-test",
        }

    def test_records_one_anonymous_unique_commit(self) -> None:
        before = hook.parse_context(self._payload("PreToolUse"))
        self.assertIsNotNone(before)
        hook.save_pre_state(before)

        (self.repo / "proof.txt").write_text("proof\n", encoding="utf-8")
        _git(self.repo, "add", "proof.txt")
        _git(self.repo, "commit", "-q", "-m", "proof")

        after = hook.parse_context(self._payload("PostToolUse"))
        self.assertIsNotNone(after)
        self.assertEqual(hook.record_post_state(after), 1)
        event = json.loads(
            (self.runtime / "codex-commit-events.jsonl").read_text(encoding="utf-8")
        )
        self.assertEqual(event["event"], "codex.git.commit")
        self.assertEqual(len(event["dedupe_id"]), 64)
        serialized_event = json.dumps(event)
        self.assertNotIn(_git(self.repo, "rev-parse", "HEAD"), serialized_event)
        self.assertNotIn(str(self.repo), serialized_event)
        self.assertNotIn("session-test", serialized_event)
        self.assertNotIn("gpt-test", serialized_event)

        hook.save_pre_state(before)
        self.assertEqual(hook.record_post_state(after), 0)
        self.assertEqual(
            len(
                (self.runtime / "codex-commit-events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ),
            1,
        )

    def test_runtime_files_are_private(self) -> None:
        dedupe_key = hook._dedupe_key(self.repo, "commit")
        self.assertEqual(hook._append_unique_events([dedupe_key]), 1)
        self.assertEqual(stat.S_IMODE(self.runtime.stat().st_mode), 0o711)
        self.assertEqual(
            stat.S_IMODE((self.runtime / "codex-commit-events.jsonl").stat().st_mode),
            0o644,
        )
        self.assertEqual(
            stat.S_IMODE((self.runtime / "codex-commit-dedupe.key").stat().st_mode),
            0o600,
        )

    def test_ignores_non_commit_command(self) -> None:
        payload = self._payload("PreToolUse", "git status --short")
        self.assertIsNone(hook.parse_context(payload))

    def test_recognizes_git_directory_option(self) -> None:
        payload = self._payload("PreToolUse", f"git -C {self.repo} commit -m test")
        context = hook.parse_context(payload)
        self.assertIsNotNone(context)
        self.assertIn(self.repo.resolve(), hook.candidate_repositories(context))

    def test_recognizes_commit_after_earlier_git_command(self) -> None:
        payload = self._payload(
            "PreToolUse",
            "git diff --check && git add proof.txt && git commit -m proof",
        )
        self.assertIsNotNone(hook.parse_context(payload))

    def test_does_not_discover_unrelated_nested_repository(self) -> None:
        workspace = self.root / "workspace"
        workspace.mkdir()
        nested = workspace / "unrelated"
        nested.mkdir()
        _git(nested, "init", "-q")

        payload = self._payload("PreToolUse")
        payload["cwd"] = str(workspace)
        context = hook.parse_context(payload)
        self.assertIsNotNone(context)
        self.assertEqual(hook.candidate_repositories(context), set())

    def test_tracks_new_repository_only_when_command_names_it(self) -> None:
        workspace = self.root / "workspace"
        workspace.mkdir()
        nested = workspace / "new-repo"
        payload = self._payload(
            "PreToolUse",
            "cd new-repo && git init && git add . && git commit -m new",
        )
        payload["cwd"] = str(workspace)
        before = hook.parse_context(payload)
        self.assertIsNotNone(before)
        hook.save_pre_state(before)

        nested.mkdir()
        _git(nested, "init", "-q")
        _git(nested, "config", "user.name", "Codex Hook Test")
        _git(nested, "config", "user.email", "codex-hook@example.invalid")
        (nested / "new.txt").write_text("new\n", encoding="utf-8")
        _git(nested, "add", "new.txt")
        _git(nested, "commit", "-q", "-m", "new repo")

        payload["hook_event_name"] = "PostToolUse"
        after = hook.parse_context(payload)
        self.assertIsNotNone(after)
        self.assertEqual(hook.record_post_state(after), 1)

    def test_dedupe_survives_malformed_audit_line(self) -> None:
        self.runtime.mkdir()
        events_path = self.runtime / "codex-commit-events.jsonl"
        events_path.write_text(
            '{"dedupe_id": "dedupe-key", "event": "codex.git.commit"}\nnot-json\n',
            encoding="utf-8",
        )
        self.assertEqual(hook._append_unique_events(["dedupe-key"]), 0)

    def test_event_retention_limit_is_enforced(self) -> None:
        self.assertEqual(
            hook._append_unique_events(
                ["first", "second", "third"],
                max_event_records=2,
            ),
            2,
        )
        lines = (
            (self.runtime / "codex-commit-events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        self.assertEqual(len(lines), 2)

    def test_stale_private_state_is_removed(self) -> None:
        state_dir = self.runtime / "codex-hook-state"
        state_dir.mkdir(parents=True)
        stale = state_dir / "stale.json"
        current = state_dir / "current.json"
        stale.write_text("{}\n", encoding="utf-8")
        current.write_text("{}\n", encoding="utf-8")
        old = time.time() - 120
        os.utime(stale, (old, old))
        self.assertEqual(hook._cleanup_stale_states(60), 1)
        self.assertFalse(stale.exists())
        self.assertTrue(current.exists())


class InstallerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.runtime = self.root / "runtime"
        self.install_dir = self.root / "install"
        self.installed_script = self.install_dir / "codex_commit_hook.py"
        self.handler = installer.hook_handler(self.installed_script, self.runtime)

    def test_preserves_existing_hooks_and_is_idempotent(self) -> None:
        config: dict[str, object] = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "*",
                        "hooks": [{"type": "command", "command": "existing"}],
                    }
                ]
            }
        }
        self.assertEqual(
            installer.install_hooks(config, handler=self.handler),
            (2, 0),
        )
        self.assertEqual(
            installer.install_hooks(config, handler=self.handler),
            (0, 0),
        )
        pre_hooks = config["hooks"]["PreToolUse"]
        self.assertEqual(pre_hooks[0]["hooks"][0]["command"], "existing")

    def test_migrates_only_known_legacy_commands(self) -> None:
        legacy = self.root / "source" / "codex_commit_hook.py"
        unrelated = self.root / "other" / "codex_commit_hook.py"
        config: dict[str, object] = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {
                                "type": "command",
                                "command": installer.hook_command(legacy, self.runtime),
                            },
                            {
                                "type": "command",
                                "command": installer.hook_command(
                                    unrelated, self.runtime
                                ),
                            },
                        ],
                    }
                ]
            }
        }
        legacy_signature = installer.hook_command(legacy, self.runtime)
        self.assertEqual(
            installer.install_hooks(
                config,
                handler=self.handler,
                owned_signatures=(legacy_signature,),
            ),
            (2, 1),
        )
        serialized = json.dumps(config)
        self.assertNotIn(str(legacy), serialized)
        self.assertIn(str(unrelated), serialized)

    def test_wrong_matcher_and_type_are_not_owned(self) -> None:
        command = self.handler["command"]
        config: dict[str, object] = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "*",
                        "hooks": [{"type": "command", "command": command}],
                    },
                    {
                        "matcher": "Bash",
                        "hooks": [{"type": "prompt", "command": command}],
                    },
                ]
            }
        }
        self.assertEqual(
            installer.install_hooks(config, handler=self.handler),
            (2, 0),
        )
        self.assertEqual(config["hooks"]["PreToolUse"][0]["matcher"], "*")
        self.assertEqual(
            config["hooks"]["PreToolUse"][1]["hooks"][0]["type"],
            "prompt",
        )

    def test_mixed_group_replacement_preserves_handler_position(self) -> None:
        legacy = "legacy exact"
        config: dict[str, object] = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {"type": "command", "command": "before"},
                            {"type": "command", "command": "legacy exact"},
                            {"type": "command", "command": "after"},
                        ],
                    }
                ]
            }
        }
        self.assertEqual(
            installer.install_hooks(
                config,
                handler=self.handler,
                owned_signatures=(legacy,),
            ),
            (2, 1),
        )
        handlers = config["hooks"]["PreToolUse"][0]["hooks"]
        self.assertEqual(handlers[0]["command"], "before")
        self.assertEqual(handlers[1], self.handler)
        self.assertEqual(handlers[2]["command"], "after")

    def test_replaces_owned_handler_with_deprecated_extra_field(self) -> None:
        previous_handler = {
            **self.handler,
            "deprecated_command": "unused-platform-command",
        }
        config: dict[str, object] = {
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Bash", "hooks": [previous_handler]},
                ]
            }
        }
        self.assertEqual(
            installer.install_hooks(config, handler=self.handler),
            (2, 1),
        )
        self.assertEqual(
            config["hooks"]["PreToolUse"][0]["hooks"],
            [self.handler],
        )

    def test_uninstall_preserves_unrelated_handlers_and_runtime(self) -> None:
        runtime_marker = self.runtime / "keep.txt"
        self.runtime.mkdir()
        runtime_marker.write_text("keep\n", encoding="utf-8")
        signature = installer._handler_signature(self.handler)
        self.assertIsNotNone(signature)
        config: dict[str, object] = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {"type": "command", "command": "before"},
                            dict(self.handler),
                            {"type": "command", "command": "after"},
                        ],
                    }
                ],
                "PostToolUse": [{"matcher": "Bash", "hooks": [dict(self.handler)]}],
            }
        }
        self.assertEqual(
            installer.uninstall_hooks(
                config,
                owned_signatures=(signature,),
            ),
            2,
        )
        self.assertEqual(
            [item["command"] for item in config["hooks"]["PreToolUse"][0]["hooks"]],
            ["before", "after"],
        )
        self.assertEqual(config["hooks"]["PostToolUse"], [])
        self.assertTrue(runtime_marker.exists())

    def test_main_uninstall_keeps_installed_script_and_runtime(self) -> None:
        hooks_file = self.root / "codex" / "hooks.json"
        arguments = [
            "--hooks-file",
            str(hooks_file),
            "--install-dir",
            str(self.install_dir),
            "--runtime-dir",
            str(self.runtime),
        ]
        with redirect_stdout(io.StringIO()):
            self.assertEqual(installer.main(arguments), 0)
        event_file = self.runtime / "codex-commit-events.jsonl"
        event_file.write_text('{"event":"codex.git.commit"}\n', encoding="utf-8")

        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(installer.main([*arguments, "--uninstall"]), 0)

        config = json.loads(hooks_file.read_text(encoding="utf-8"))
        self.assertEqual(config["hooks"]["PreToolUse"], [])
        self.assertEqual(config["hooks"]["PostToolUse"], [])
        self.assertTrue(self.installed_script.exists())
        self.assertTrue(event_file.exists())
        self.assertIn("uninstalled=2", output.getvalue())
        self.assertIn("preserved_runtime_dir=", output.getvalue())

    def test_main_copies_script_records_hash_and_backs_up_config(self) -> None:
        hooks_file = self.root / "codex" / "hooks.json"
        hooks_file.parent.mkdir()
        hooks_file.write_text(
            json.dumps({"hooks": {"Notification": [{"matcher": "*"}]}}),
            encoding="utf-8",
        )

        with quiet():
            result = installer.main(
                [
                    "--hooks-file",
                    str(hooks_file),
                    "--hook-script",
                    str(HOOK_PATH),
                    "--install-dir",
                    str(self.install_dir),
                    "--runtime-dir",
                    str(self.runtime),
                ]
            )
        self.assertEqual(result, 0)
        self.assertEqual(self.installed_script.read_bytes(), HOOK_PATH.read_bytes())
        metadata = json.loads(
            (self.install_dir / "install.json").read_text(encoding="utf-8")
        )
        self.assertEqual(metadata["runtime_dir"], str(self.runtime.resolve()))
        self.assertEqual(len(metadata["sha256"]), 64)
        self.assertEqual(
            metadata["handler_signatures"],
            [installer._handler_signature(self.handler)],
        )
        self.assertEqual(len(list(hooks_file.parent.glob("hooks.json.backup-*"))), 1)
        config = json.loads(hooks_file.read_text(encoding="utf-8"))
        self.assertIn("Notification", config["hooks"])

    def test_changed_hook_requires_explicit_update(self) -> None:
        hooks_file = self.root / "codex" / "hooks.json"
        source = self.root / "source.py"
        source.write_text("print('one')\n", encoding="utf-8")
        arguments = [
            "--hooks-file",
            str(hooks_file),
            "--hook-script",
            str(source),
            "--install-dir",
            str(self.install_dir),
            "--runtime-dir",
            str(self.runtime),
        ]
        with quiet():
            self.assertEqual(installer.main(arguments), 0)
        source.write_text("print('two')\n", encoding="utf-8")

        with quiet(), self.assertRaises(SystemExit) as raised:
            installer.main(arguments)
        self.assertEqual(raised.exception.code, 2)
        with quiet():
            self.assertEqual(installer.main([*arguments, "--update"]), 0)
        self.assertEqual(
            self.installed_script.read_text(encoding="utf-8"),
            "print('two')\n",
        )

    def test_installed_files_are_private(self) -> None:
        hooks_file = self.root / "codex" / "hooks.json"
        with quiet():
            installer.main(
                [
                    "--hooks-file",
                    str(hooks_file),
                    "--install-dir",
                    str(self.install_dir),
                    "--runtime-dir",
                    str(self.runtime),
                ]
            )
        self.assertEqual(stat.S_IMODE(self.install_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.runtime.stat().st_mode), 0o711)
        self.assertEqual(stat.S_IMODE(self.installed_script.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(hooks_file.stat().st_mode), 0o600)
        self.assertEqual(
            stat.S_IMODE((self.runtime / "codex-commit-events.jsonl").stat().st_mode),
            0o644,
        )


if __name__ == "__main__":
    unittest.main()
