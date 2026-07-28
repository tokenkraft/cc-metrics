#!/usr/bin/env python3
"""Count Git commits created during commit-capable Codex Bash tool calls."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

GIT_TIMEOUT_SECONDS = 5
LOCK_TIMEOUT_SECONDS = 5
STALE_LOCK_SECONDS = 30
COMMIT_CLOCK_TOLERANCE_SECONDS = 5
DEFAULT_STATE_MAX_AGE_SECONDS = 86_400
DEFAULT_MAX_EVENT_RECORDS = 10_000
COMMIT_SUBCOMMANDS = frozenset(
    {"am", "cherry-pick", "commit", "merge", "pull", "rebase", "revert"}
)
_RUNTIME_DIR_OVERRIDE: Path | None = None
_STATE_MAX_AGE_SECONDS = DEFAULT_STATE_MAX_AGE_SECONDS
_MAX_EVENT_RECORDS = DEFAULT_MAX_EVENT_RECORDS


def _supported_platform() -> bool:
    return sys.platform == "darwin" or sys.platform.startswith("linux")


def _positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


@dataclass(frozen=True)
class HookContext:
    """Validated fields needed from Codex hook input."""

    event: str
    invocation_id: str
    command: str
    cwd: Path


class FileLock:
    """Small lock based on atomic file creation."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._descriptor: int | None = None

    def __enter__(self) -> FileLock:
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                self._descriptor = os.open(
                    self.path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
                os.write(self._descriptor, f"{os.getpid()}\n".encode())
                return self
            except FileExistsError:
                try:
                    lock_age = time.time() - self.path.stat().st_mtime
                    if lock_age > STALE_LOCK_SECONDS:
                        self.path.unlink()
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out waiting for lock: {self.path}")
                time.sleep(0.05)

    def __exit__(self, *_: object) -> None:
        if self._descriptor is not None:
            os.close(self._descriptor)
            self._descriptor = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def default_runtime_dir() -> Path:
    """Return user-owned state directory for macOS or Ubuntu Linux."""

    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/cc-metrics/runtime"
    xdg_state_home = os.environ.get("XDG_STATE_HOME")
    base = (
        Path(xdg_state_home).expanduser()
        if xdg_state_home
        else Path.home() / ".local/state"
    )
    return base / "cc-metrics/runtime"


def _runtime_dir() -> Path:
    if _RUNTIME_DIR_OVERRIDE is not None:
        return _RUNTIME_DIR_OVERRIDE
    configured = os.environ.get("CC_METRICS_RUNTIME_DIR")
    return Path(configured).expanduser() if configured else default_runtime_dir()


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _ensure_runtime_directory(path: Path) -> None:
    """Allow collector traversal while keeping sensitive children private."""

    path.mkdir(parents=True, exist_ok=True, mode=0o711)
    try:
        path.chmod(0o711)
    except OSError:
        pass


def _ensure_file_mode(path: Path, mode: int = 0o600) -> None:
    try:
        path.chmod(mode)
    except OSError:
        pass


def _string_field(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    return value if isinstance(value, str) else ""


def parse_context(data: Mapping[str, Any]) -> HookContext | None:
    """Return validated hook context, or None for irrelevant hook events."""

    event = _string_field(data, "hook_event_name")
    if event not in {"PreToolUse", "PostToolUse"}:
        return None
    if _string_field(data, "tool_name") != "Bash":
        return None

    tool_input = data.get("tool_input")
    if not isinstance(tool_input, Mapping):
        return None
    command = _string_field(tool_input, "command")
    cwd_text = _string_field(data, "cwd")
    if not command or not cwd_text or not command_may_create_commit(command):
        return None

    invocation_seed = "\0".join(
        (
            _string_field(data, "tool_use_id"),
            _string_field(data, "turn_id"),
            command,
            cwd_text,
        )
    )
    invocation_id = hashlib.sha256(invocation_seed.encode()).hexdigest()[:24]
    return HookContext(
        event=event,
        invocation_id=invocation_id,
        command=command,
        cwd=Path(cwd_text).expanduser(),
    )


def _shell_tokens(command: str) -> list[str]:
    """Split POSIX shell text without executing it."""

    try:
        return shlex.split(command, comments=True, posix=True)
    except ValueError:
        return command.replace(";", " ").replace("&&", " ").split()


def _is_git_executable(token: str) -> bool:
    return Path(token).name == "git"


def command_may_create_commit(command: str) -> bool:
    """Return whether shell text contains a supported Git commit operation."""

    tokens = _shell_tokens(command)

    options_with_values = {
        "-C",
        "-c",
        "--config-env",
        "--exec-path",
        "--git-dir",
        "--namespace",
        "--super-prefix",
        "--work-tree",
    }
    for index, token in enumerate(tokens):
        if not _is_git_executable(token):
            continue
        candidate_index = index + 1
        while candidate_index < len(tokens):
            candidate = tokens[candidate_index]
            if candidate in {"&&", "||", ";", "|"}:
                break
            if candidate in options_with_values:
                candidate_index += 2
                continue
            if candidate.startswith("-"):
                candidate_index += 1
                continue
            if candidate in COMMIT_SUBCOMMANDS:
                return True
            break
    return False


def _run_git(arguments: list[str], cwd: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def git_root(path: Path) -> Path | None:
    """Resolve Git worktree root containing path."""

    if not path.is_dir():
        return None
    root = _run_git(["rev-parse", "--show-toplevel"], path)
    return Path(root).resolve() if root else None


def git_head(root: Path) -> str | None:
    """Return current commit SHA, including None for an unborn repository."""

    return _run_git(["rev-parse", "HEAD"], root)


def _command_paths(
    command: str,
    cwd: Path,
) -> set[Path]:
    """Return only paths explicitly named by cwd, cd, or git -C."""

    tokens = _shell_tokens(command)
    paths = {cwd}

    for index, token in enumerate(tokens[:-1]):
        if token == "cd":
            path = Path(tokens[index + 1]).expanduser()
            paths.add(path if path.is_absolute() else cwd / path)
        if not _is_git_executable(token):
            continue
        candidate_index = index + 1
        while candidate_index < len(tokens) - 1:
            candidate = tokens[candidate_index]
            if candidate in {"&&", "||", ";", "|"}:
                break
            if candidate == "-C":
                path = Path(tokens[candidate_index + 1]).expanduser()
                paths.add(path if path.is_absolute() else cwd / path)
                candidate_index += 2
                continue
            if candidate.startswith("-"):
                candidate_index += 1
                continue
            break
    return paths


def candidate_repositories(context: HookContext) -> set[Path]:
    """Find repositories explicitly named by cwd or command."""

    return {
        root
        for path in _command_paths(context.command, context.cwd)
        if (root := git_root(Path(path)))
    }


def _state_path(context: HookContext) -> Path:
    return _runtime_dir() / "codex-hook-state" / f"{context.invocation_id}.json"


def _atomic_json_write(path: Path, data: Mapping[str, Any]) -> None:
    _ensure_private_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}."
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        _ensure_file_mode(path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def _cleanup_stale_states(max_age_seconds: int, *, now: float | None = None) -> int:
    """Remove expired private snapshots containing repository paths."""

    state_dir = _runtime_dir() / "codex-hook-state"
    cutoff = (time.time() if now is None else now) - max_age_seconds
    removed = 0
    try:
        candidates = tuple(state_dir.glob("*.json"))
    except OSError:
        return 0
    for candidate in candidates:
        try:
            metadata = os.stat(candidate, follow_symlinks=False)
            if stat.S_ISREG(metadata.st_mode) and metadata.st_mtime < cutoff:
                candidate.unlink()
                removed += 1
        except FileNotFoundError:
            continue
        except OSError:
            continue
    return removed


def save_pre_state(context: HookContext) -> None:
    """Snapshot repository HEADs before Codex runs a commit-capable command."""

    _cleanup_stale_states(_STATE_MAX_AGE_SECONDS)
    repositories = candidate_repositories(context)
    _atomic_json_write(
        _state_path(context),
        {
            "started_at": int(time.time()),
            "repos": {str(root): git_head(root) for root in sorted(repositories)},
        },
    )


def _discard(path: Path) -> None:
    """Drop unusable pre-state so the next commit starts clean.

    Best-effort: a state file we cannot read is already useless, and failing to
    remove it must not fail the commit the hook is attached to.
    """
    try:
        path.unlink()
    except OSError:
        pass


def _load_pre_state(context: HookContext) -> tuple[int, dict[str, str | None]] | None:
    path = _state_path(context)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError):
        _discard(path)
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("started_at"), int):
        _discard(path)
        return None
    repos = raw.get("repos")
    if not isinstance(repos, dict):
        _discard(path)
        return None
    validated: dict[str, str | None] = {}
    for key, value in repos.items():
        if isinstance(key, str) and (isinstance(value, str) or value is None):
            validated[key] = value
    return raw["started_at"], validated


def _new_commits(root: Path, before: str | None, after: str) -> list[str]:
    arguments = ["rev-list", "--reverse", after]
    if before:
        arguments.append(f"^{before}")
    output = _run_git(arguments, root)
    return output.splitlines() if output else []


def _commit_timestamp(root: Path, commit: str) -> int | None:
    value = _run_git(["show", "-s", "--format=%ct", commit], root)
    try:
        return int(value) if value else None
    except ValueError:
        return None


def _dedupe_secret() -> bytes:
    runtime_dir = _runtime_dir()
    _ensure_runtime_directory(runtime_dir)
    secret_path = runtime_dir / "codex-commit-dedupe.key"
    lock_path = runtime_dir / "codex-commit-dedupe.lock"
    with FileLock(lock_path):
        try:
            secret = secret_path.read_bytes()
        except OSError:
            secret = b""
        if len(secret) != 32:
            secret = secrets.token_bytes(32)
            descriptor, temporary_name = tempfile.mkstemp(
                dir=runtime_dir,
                prefix=f".{secret_path.name}.",
            )
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(secret)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_name, secret_path)
                _ensure_file_mode(secret_path)
            finally:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass
        return secret


def _dedupe_key(root: Path, commit: str, secret: bytes | None = None) -> str:
    """Derive the anonymous per-commit key.

    ``secret`` is accepted so a caller keying several commits reads the key file
    once instead of once per commit; omitted, it is fetched per call.
    """
    if secret is None:
        secret = _dedupe_secret()
    material = f"{root}\0{commit}".encode()
    return hmac.new(secret, material, hashlib.sha256).hexdigest()


def _append_unique_events(
    dedupe_keys: Iterable[str],
    *,
    max_event_records: int | None = None,
) -> int:
    """Append anonymous count events for previously unseen commit hashes."""

    record_limit = (
        _MAX_EVENT_RECORDS if max_event_records is None else max_event_records
    )
    runtime_dir = _runtime_dir()
    _ensure_runtime_directory(runtime_dir)
    events_path = runtime_dir / "codex-commit-events.jsonl"
    lock_path = runtime_dir / "codex-commit-events.lock"
    appended = 0
    with FileLock(lock_path):
        try:
            existing_lines = events_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            existing_lines = []
        seen: set[str] = set()
        record_count = 0
        for line in existing_lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and isinstance(event.get("dedupe_id"), str):
                seen.add(event["dedupe_id"])
                record_count += 1

        with events_path.open("a", encoding="utf-8") as events_handle:
            _ensure_file_mode(events_path, 0o644)
            for dedupe_key in dedupe_keys:
                if dedupe_key in seen:
                    continue
                if record_count >= record_limit:
                    break
                json.dump(
                    {
                        "dedupe_id": dedupe_key,
                        "event": "codex.git.commit",
                    },
                    events_handle,
                    sort_keys=True,
                )
                events_handle.write("\n")
                events_handle.flush()
                os.fsync(events_handle.fileno())
                seen.add(dedupe_key)
                appended += 1
                record_count += 1
    return appended


def record_post_state(context: HookContext) -> int:
    """Compare explicit repository HEADs and append anonymous count events."""

    _cleanup_stale_states(_STATE_MAX_AGE_SECONDS)
    state_path = _state_path(context)
    loaded = _load_pre_state(context)
    if loaded is None:
        return 0
    started_at, before_heads = loaded
    ended_at = int(time.time())
    repositories = candidate_repositories(context)
    repositories.update(Path(path) for path in before_heads)
    dedupe_keys: list[str] = []
    # Read the key file at most once. A rebase or pull lands many commits in one
    # hook invocation, and each read takes the dedupe lock; fetching lazily keeps
    # the common no-new-commits case at zero reads.
    secret: bytes | None = None

    for root in sorted(repositories):
        after = git_head(root)
        before = before_heads.get(str(root))
        if not after or after == before:
            continue
        for commit in _new_commits(root, before, after):
            committed_at = _commit_timestamp(root, commit)
            if committed_at is None:
                continue
            if committed_at < started_at - COMMIT_CLOCK_TOLERANCE_SECONDS:
                continue
            if committed_at > ended_at + COMMIT_CLOCK_TOLERANCE_SECONDS:
                continue
            if secret is None:
                secret = _dedupe_secret()
            dedupe_keys.append(_dedupe_key(root, commit, secret))

    appended = _append_unique_events(dedupe_keys)
    try:
        state_path.unlink()
    except OSError:
        pass
    return appended


def _log_error(message: str) -> None:
    try:
        runtime_dir = _runtime_dir()
        _ensure_runtime_directory(runtime_dir)
        path = runtime_dir / "codex-commit-hook-errors.log"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{int(time.time())} {message}\n")
        _ensure_file_mode(path)
    except OSError:
        pass


def main(argv: Sequence[str] | None = None) -> int:
    """Read Codex hook JSON from stdin and fail open on all hook errors."""

    if not _supported_platform():
        return 0

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path)
    parser.add_argument(
        "--state-max-age-seconds",
        type=_positive_integer,
        default=DEFAULT_STATE_MAX_AGE_SECONDS,
    )
    parser.add_argument(
        "--max-event-records",
        type=_positive_integer,
        default=DEFAULT_MAX_EVENT_RECORDS,
    )
    arguments = parser.parse_args(argv)

    global _MAX_EVENT_RECORDS, _RUNTIME_DIR_OVERRIDE, _STATE_MAX_AGE_SECONDS
    if arguments.runtime_dir is not None:
        _RUNTIME_DIR_OVERRIDE = arguments.runtime_dir.expanduser().resolve()
    _STATE_MAX_AGE_SECONDS = arguments.state_max_age_seconds
    _MAX_EVENT_RECORDS = arguments.max_event_records

    try:
        data = json.load(sys.stdin)
        if not isinstance(data, dict):
            return 0
        context = parse_context(data)
        if context is None:
            return 0
        if context.event == "PreToolUse":
            save_pre_state(context)
        else:
            record_post_state(context)
    except Exception as error:  # Hook must never block Codex tool execution.
        _log_error(f"{type(error).__name__}: {error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
