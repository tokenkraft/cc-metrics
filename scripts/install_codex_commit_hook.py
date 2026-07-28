#!/usr/bin/env python3
"""Install a private copy of the cc-metrics Codex commit hook."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import sys
import tempfile
import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

DEFAULT_HOOKS_FILE = Path.home() / ".codex" / "hooks.json"
SOURCE_HOOK_SCRIPT = Path(__file__).resolve().with_name("codex_commit_hook.py")
DEFAULT_STATE_MAX_AGE_SECONDS = 86_400
DEFAULT_MAX_EVENT_RECORDS = 10_000
LOCK_TIMEOUT_SECONDS = 10
STALE_LOCK_SECONDS = 60
EVENT_NAMES = ("PreToolUse", "PostToolUse")


def _supported_platform() -> bool:
    return sys.platform == "darwin" or sys.platform.startswith("linux")


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


def default_install_dir() -> Path:
    """Return user-owned hook installation directory for macOS or Ubuntu Linux."""

    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/cc-metrics/hooks"
    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    base = (
        Path(xdg_data_home).expanduser()
        if xdg_data_home
        else Path.home() / ".local/share"
    )
    return base / "cc-metrics/hooks"


def default_runtime_dir() -> Path:
    """Return user-owned runtime directory for macOS or Ubuntu Linux."""

    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/cc-metrics/runtime"
    xdg_state_home = os.environ.get("XDG_STATE_HOME")
    base = (
        Path(xdg_state_home).expanduser()
        if xdg_state_home
        else Path.home() / ".local/state"
    )
    return base / "cc-metrics/runtime"


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


def _ensure_private_file(path: Path, mode: int = 0o600) -> None:
    try:
        path.chmod(mode)
    except OSError:
        pass


def _load_hooks(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"hooks": {}}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a JSON object")
    hooks = raw.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f'{path} field "hooks" must be a JSON object')
    return raw


def _positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _hook_arguments(
    hook_script: Path,
    runtime_dir: Path,
    *,
    interpreter: Path | None,
    state_max_age_seconds: int,
    max_event_records: int,
) -> list[str]:
    python = (interpreter or Path(sys.executable)).resolve()
    return [
        str(python),
        str(hook_script.resolve()),
        "--runtime-dir",
        str(runtime_dir.resolve()),
        "--state-max-age-seconds",
        str(state_max_age_seconds),
        "--max-event-records",
        str(max_event_records),
    ]


def hook_command(
    hook_script: Path,
    runtime_dir: Path,
    *,
    interpreter: Path | None = None,
    state_max_age_seconds: int = DEFAULT_STATE_MAX_AGE_SECONDS,
    max_event_records: int = DEFAULT_MAX_EVENT_RECORDS,
) -> str:
    """Build POSIX hook command using absolute executable and data paths."""

    return shlex.join(
        _hook_arguments(
            hook_script,
            runtime_dir,
            interpreter=interpreter,
            state_max_age_seconds=state_max_age_seconds,
            max_event_records=max_event_records,
        )
    )


def hook_handler(
    hook_script: Path,
    runtime_dir: Path,
    *,
    interpreter: Path | None = None,
    state_max_age_seconds: int = DEFAULT_STATE_MAX_AGE_SECONDS,
    max_event_records: int = DEFAULT_MAX_EVENT_RECORDS,
) -> dict[str, str]:
    """Build POSIX Codex command handler."""

    return {
        "type": "command",
        "command": hook_command(
            hook_script,
            runtime_dir,
            interpreter=interpreter,
            state_max_age_seconds=state_max_age_seconds,
            max_event_records=max_event_records,
        ),
    }


def _handler_signature(handler: object) -> str | None:
    if not isinstance(handler, dict) or handler.get("type") != "command":
        return None
    command = handler.get("command")
    return command if isinstance(command, str) else None


def _legacy_signatures(
    scripts: Iterable[Path],
    runtime_dir: Path,
    *,
    interpreter: Path | None,
) -> set[str]:
    signatures: set[str] = set()
    python = (interpreter or Path(sys.executable)).resolve()
    for script in scripts:
        resolved = script.resolve()
        old_arguments = [
            str(python),
            str(resolved),
            "--runtime-dir",
            str(runtime_dir.resolve()),
        ]
        signatures.add(shlex.join(old_arguments))
        signatures.add(f"python3 {shlex.quote(str(resolved))}")
    return signatures


def install_hooks(
    config: dict[str, Any],
    *,
    handler: Mapping[str, str],
    owned_signatures: Iterable[str] = (),
) -> tuple[int, int]:
    """Install both hook entries and replace exact owned Bash handlers."""

    hooks = config.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError('field "hooks" must be a JSON object')
    current_signature = _handler_signature(handler)
    if current_signature is None:
        raise ValueError("installed handler must be a command")
    owned = set(owned_signatures)
    owned.add(current_signature)
    added = 0
    removed = 0

    for event_name in EVENT_NAMES:
        entries = hooks.setdefault(event_name, [])
        if not isinstance(entries, list):
            raise ValueError(f'field "hooks.{event_name}" must be a JSON array')

        already_installed = False
        insertion: tuple[int, int] | None = None
        updated_entries: list[Any] = []
        for entry_index, entry in enumerate(entries):
            if (
                not isinstance(entry, dict)
                or entry.get("matcher") != "Bash"
                or not isinstance(entry.get("hooks"), list)
            ):
                updated_entries.append(entry)
                continue

            retained_handlers: list[Any] = []
            for handler_index, existing_handler in enumerate(entry["hooks"]):
                signature = _handler_signature(existing_handler)
                if existing_handler == dict(handler) and not already_installed:
                    already_installed = True
                    retained_handlers.append(existing_handler)
                    continue
                if signature in owned:
                    if insertion is None:
                        insertion = (entry_index, len(retained_handlers))
                    removed += 1
                else:
                    retained_handlers.append(existing_handler)

            updated_entry = dict(entry)
            updated_entry["hooks"] = retained_handlers
            updated_entries.append(updated_entry)

        if not already_installed:
            if insertion is None:
                updated_entries.append({"matcher": "Bash", "hooks": [dict(handler)]})
            else:
                entry_index, handler_index = insertion
                target = updated_entries[entry_index]
                target["hooks"].insert(handler_index, dict(handler))
            added += 1
        hooks[event_name] = updated_entries
    return added, removed


def uninstall_hooks(
    config: dict[str, Any],
    *,
    owned_signatures: Iterable[str],
) -> int:
    """Remove exact owned Bash handlers while preserving all other config."""

    hooks = config.get("hooks")
    if not isinstance(hooks, dict):
        raise ValueError('field "hooks" must be a JSON object')
    owned = set(owned_signatures)
    removed = 0
    for event_name in EVENT_NAMES:
        entries = hooks.get(event_name, [])
        if not isinstance(entries, list):
            raise ValueError(f'field "hooks.{event_name}" must be a JSON array')
        retained_entries: list[Any] = []
        for entry in entries:
            if (
                not isinstance(entry, dict)
                or entry.get("matcher") != "Bash"
                or not isinstance(entry.get("hooks"), list)
            ):
                retained_entries.append(entry)
                continue
            retained_handlers = []
            for existing_handler in entry["hooks"]:
                if _handler_signature(existing_handler) in owned:
                    removed += 1
                else:
                    retained_handlers.append(existing_handler)
            if retained_handlers:
                updated_entry = dict(entry)
                updated_entry["hooks"] = retained_handlers
                retained_entries.append(updated_entry)
        hooks[event_name] = retained_entries
    return removed


def _atomic_write_bytes(path: Path, content: bytes, mode: int) -> None:
    _ensure_private_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}."
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, mode)
        os.replace(temporary_name, path)
        _ensure_private_file(path, mode)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def _atomic_write_json(path: Path, config: dict[str, Any]) -> None:
    content = (json.dumps(config, indent=2) + "\n").encode()
    _atomic_write_bytes(path, content, 0o600)


def _backup(path: Path) -> Path:
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup = path.with_name(f"{path.name}.backup-{timestamp}")
    suffix = 1
    while backup.exists():
        backup = path.with_name(f"{path.name}.backup-{timestamp}-{suffix}")
        suffix += 1
    shutil.copy2(path, backup)
    _ensure_private_file(backup)
    return backup


def _install_script(source: Path, destination: Path) -> str:
    content = source.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    _atomic_write_bytes(destination, content, 0o700)
    return digest


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _installed_digest(metadata_file: Path) -> str | None:
    try:
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(metadata, dict):
        return None
    digest = metadata.get("sha256")
    return digest if isinstance(digest, str) else None


def _stored_signatures(metadata_file: Path) -> set[str]:
    try:
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return set()
    if not isinstance(metadata, dict) or not isinstance(
        metadata.get("handler_signatures"), list
    ):
        return set()
    signatures: set[str] = set()
    for value in metadata["handler_signatures"]:
        if isinstance(value, str):
            signatures.add(value)
        elif isinstance(value, list) and value and isinstance(value[0], str):
            signatures.add(value[0])
    return signatures


def main(argv: Sequence[str] | None = None) -> int:
    if not _supported_platform():
        print(
            "unsupported platform; supported: macOS and Ubuntu Linux",
            file=sys.stderr,
        )
        return 2

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hooks-file", type=Path, default=DEFAULT_HOOKS_FILE)
    parser.add_argument("--hook-script", type=Path, default=SOURCE_HOOK_SCRIPT)
    parser.add_argument("--install-dir", type=Path, default=default_install_dir())
    parser.add_argument("--runtime-dir", type=Path, default=default_runtime_dir())
    parser.add_argument(
        "--legacy-hook-script",
        action="append",
        default=[],
        type=Path,
        help="additional prior hook path to migrate; may be repeated",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="confirm replacement when installed hook content changed",
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="remove owned hook entries; preserve installed files and runtime data",
    )
    parser.add_argument(
        "--state-max-age-seconds",
        type=_positive_integer,
        default=DEFAULT_STATE_MAX_AGE_SECONDS,
        help="maximum age of private PreToolUse snapshots",
    )
    parser.add_argument(
        "--max-event-records",
        type=_positive_integer,
        default=DEFAULT_MAX_EVENT_RECORDS,
        help="maximum anonymous commit records retained",
    )
    arguments = parser.parse_args(argv)
    if arguments.update and arguments.uninstall:
        parser.error("--update and --uninstall are mutually exclusive")

    source = arguments.hook_script.expanduser().resolve()
    hooks_file = arguments.hooks_file.expanduser().resolve()
    install_dir = arguments.install_dir.expanduser().resolve()
    runtime_dir = arguments.runtime_dir.expanduser().resolve()
    installed_script = install_dir / "codex_commit_hook.py"
    metadata_file = install_dir / "install.json"

    if not arguments.uninstall and not source.is_file():
        parser.error(f"hook script not found: {source}")

    event_file = runtime_dir / "codex-commit-events.jsonl"
    _ensure_private_directory(hooks_file.parent)
    lock_file = hooks_file.with_name(f".{hooks_file.name}.lock")
    previous_digest = _installed_digest(metadata_file)
    legacy_scripts = [
        source,
        installed_script,
        *(path.expanduser().resolve() for path in arguments.legacy_hook_script),
    ]
    handler = hook_handler(
        installed_script,
        runtime_dir,
        state_max_age_seconds=arguments.state_max_age_seconds,
        max_event_records=arguments.max_event_records,
    )
    current_signature = _handler_signature(handler)
    if current_signature is None:
        parser.error("generated invalid hook handler")
    owned_signatures = _legacy_signatures(
        legacy_scripts,
        runtime_dir,
        interpreter=None,
    )
    owned_signatures.update(_stored_signatures(metadata_file))
    owned_signatures.add(current_signature)

    if arguments.uninstall:
        try:
            with FileLock(lock_file):
                config = _load_hooks(hooks_file)
                removed = uninstall_hooks(
                    config,
                    owned_signatures=owned_signatures,
                )
                backup: Path | None = None
                if removed:
                    if hooks_file.exists():
                        backup = _backup(hooks_file)
                    _atomic_write_json(hooks_file, config)
        except (OSError, TimeoutError, json.JSONDecodeError, ValueError) as error:
            parser.error(str(error))
        backup_text = str(backup) if backup is not None else "none"
        print(
            f"uninstalled={removed} hooks_file={hooks_file} backup={backup_text} "
            f"preserved_install_dir={install_dir} preserved_runtime_dir={runtime_dir}"
        )
        return 0

    _ensure_private_directory(install_dir)
    _ensure_runtime_directory(runtime_dir)
    try:
        event_descriptor = os.open(
            event_file,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o644,
        )
    except FileExistsError:
        pass
    else:
        os.close(event_descriptor)
    _ensure_private_file(event_file, 0o644)
    source_digest = _sha256(source)
    if (
        previous_digest is not None
        and previous_digest != source_digest
        and not arguments.update
    ):
        parser.error(
            "installed hook differs from source; review hashes and rerun with "
            f"--update (installed={previous_digest}, source={source_digest})"
        )

    try:
        with FileLock(lock_file):
            digest = _install_script(source, installed_script)
            config = _load_hooks(hooks_file)
            added, removed = install_hooks(
                config,
                handler=handler,
                owned_signatures=owned_signatures,
            )
            backup: Path | None = None
            if added or removed:
                if hooks_file.exists():
                    backup = _backup(hooks_file)
                _atomic_write_json(hooks_file, config)
            _atomic_write_json(
                metadata_file,
                {
                    "hook_script": str(installed_script),
                    "runtime_dir": str(runtime_dir),
                    "sha256": digest,
                    "handler_signatures": [current_signature],
                },
            )
    except (OSError, TimeoutError, json.JSONDecodeError, ValueError) as error:
        parser.error(str(error))

    backup_text = str(backup) if backup is not None else "none"
    print(
        f"installed={added} migrated={removed} hooks_file={hooks_file} "
        f"backup={backup_text} runtime_dir={runtime_dir} "
        f"previous_sha256={previous_digest or 'none'} sha256={digest}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
