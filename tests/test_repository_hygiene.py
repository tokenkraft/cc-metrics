from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
TROUBLESHOOTING = ROOT / "troubleshooting.md"
LICENSE = ROOT / "LICENSE"
CONTRIBUTING = ROOT / "CONTRIBUTING.md"
SECURITY = ROOT / "SECURITY.md"
DOCS_DIR = ROOT / "docs"
OPERATIONS = DOCS_DIR / "operations.md"
CODEX_LEDGER = DOCS_DIR / "codex-ledger.md"
METRICS_CONTRACT = DOCS_DIR / "metrics-contract.md"
# The README is the landing page: setup and pointers. Internals live in docs/.
README_MAX_WORDS = 1500
ENV_TEMPLATE = ROOT / ".env.example"
COMPOSE = ROOT / "docker-compose.yml"
COLLECTOR = ROOT / "otel-collector-config.yaml"

CANONICAL_MIT_TEXT = """MIT License

Copyright (c) 2026 cc-metrics contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE."""


BUILD_ARTIFACTS = {".git", "__pycache__", ".ruff_cache", ".pytest_cache"}
# Created by following the setup steps; ignored in a checkout, so the fallback
# has to skip them too or it scans the reader's own credentials.
LOCAL_STATE = {".secrets", "runtime"}


def public_files() -> list[Path]:
    """Files that ship with the repository.

    Uses ``git ls-files`` so ``.gitignore`` stays the single source of truth for
    what is local-only. Falls back to a plain walk when there is no repository -
    e.g. a downloaded archive - in which case build artifacts are skipped
    explicitly, since there is no ignore file to consult.
    """
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=False,
        capture_output=True,
    )
    if result.returncode == 0:
        return [
            ROOT / path.decode()
            for path in result.stdout.split(b"\0")
            if path and not path.startswith(b".git/")
        ]
    return [
        path
        for path in ROOT.rglob("*")
        if path.is_file()
        and not BUILD_ARTIFACTS & set(path.relative_to(ROOT).parts)
        and not LOCAL_STATE & set(path.relative_to(ROOT).parts)
        and not path.name.endswith(".pyc")
        and not (path.name.startswith(".env") and path.name != ".env.example")
    ]


def environment_values() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in ENV_TEMPLATE.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    return values


class RepositoryHygieneTests(unittest.TestCase):
    def assert_no_match_in_public_files(
        self, patterns: tuple[re.Pattern[str], ...]
    ) -> None:
        """Assert none of ``patterns`` appears in any shipped text file.

        Binary files - images - cannot contain these and are skipped on decode.
        """
        for path in public_files():
            if not path.is_file():
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            relative = path.relative_to(ROOT).as_posix()
            for pattern in patterns:
                with self.subTest(path=relative, pattern=pattern.pattern):
                    self.assertIsNone(pattern.search(content))

    @staticmethod
    def shipped_markdown() -> list[Path]:
        return [ROOT / name for name in sorted(ROOT.glob("*.md"))] + sorted(
            DOCS_DIR.rglob("*.md")
        )

    @staticmethod
    def heading_anchors(document: Path) -> set[str]:
        """GitHub-style anchors: lowercase, punctuation dropped, spaces to dashes."""
        anchors = set()
        for line in document.read_text(encoding="utf-8").splitlines():
            if not line.startswith("#"):
                continue
            text = line.lstrip("#").strip().lower()
            text = re.sub(r"[^\w\s-]", "", text)
            anchors.add(re.sub(r"\s+", "-", text))
        return anchors

    def test_shipped_markdown_relative_links_and_fragments_resolve(self) -> None:
        documents = self.shipped_markdown()
        self.assertIn(OPERATIONS, documents)
        for document in documents:
            links = re.findall(
                r"\[[^\]]+\]\(([^)]+)\)",
                document.read_text(encoding="utf-8"),
            )
            for link in links:
                if "://" in link or link.startswith("mailto:"):
                    continue
                target_part, _, fragment = link.partition("#")
                target = document if not target_part else document.parent / target_part
                with self.subTest(document=document.name, link=link):
                    self.assertTrue(target.exists())
                    if fragment and target.suffix == ".md":
                        self.assertIn(fragment, self.heading_anchors(target))

    def test_public_files_have_no_personal_paths_or_weak_passwords(self) -> None:
        self.assert_no_match_in_public_files(
            (
                re.compile("/" + r"Users/[^/\s]+/"),
                re.compile("/" + r"home/[^/\s]+/"),
                re.compile(r"\b[A-Z]:\\Users\\[^\\\s]+\\"),
                re.compile(r"\bchangeme\b", re.IGNORECASE),
            )
        )

    def test_public_files_carry_no_identifying_email_address(self) -> None:
        """Only addresses that are safe by construction, e.g. reserved domains."""
        address = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
        # GitHub's noreply form, plus the RFC 2606 / RFC 6761 reserved names that
        # exist precisely so documentation and fixtures can name an address safely.
        allowed_domains = (
            "users.noreply.github.com",
            ".invalid",
            ".test",
            ".example",
            ".localhost",
            "example.com",
            "example.net",
            "example.org",
        )
        for path in public_files():
            if not path.is_file():
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            relative = path.relative_to(ROOT).as_posix()
            for match in address.finditer(content):
                found = match.group(0)
                if found.endswith(allowed_domains):
                    continue
                with self.subTest(path=relative):
                    self.fail(
                        f"{relative} contains an email address "
                        f"(domain {found.rsplit('@', 1)[1]}). Use a reserved domain "
                        f"such as example.invalid in fixtures and docs."
                    )

    def test_public_files_have_no_common_secret_formats(self) -> None:
        self.assert_no_match_in_public_files(
            (
                re.compile(r"-----BEGIN (?:EC |OPENSSH |RSA )?PRIVATE KEY-----"),
                re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
                re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"),
                re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
                re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
            )
        )

    def test_required_public_documents_exist(self) -> None:
        for path in (
            README,
            TROUBLESHOOTING,
            LICENSE,
            CONTRIBUTING,
            SECURITY,
            OPERATIONS,
            CODEX_LEDGER,
            METRICS_CONTRACT,
        ):
            with self.subTest(path=path.name):
                self.assertTrue(path.is_file())
                self.assertTrue(path.read_text(encoding="utf-8").strip())

    def test_license_contains_unmodified_mit_text(self) -> None:
        self.assertIn(CANONICAL_MIT_TEXT, LICENSE.read_text(encoding="utf-8"))

    def test_public_documentation_contract(self) -> None:
        readme = README.read_text(encoding="utf-8")
        self.assertIn("Claude Code | 2.1.214", readme)
        self.assertIn("Codex CLI | 0.145.0", readme)
        self.assertIn("macOS", readme)
        self.assertIn("Ubuntu", readme)
        self.assertIn(
            "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=delta",
            readme,
        )
        contract = METRICS_CONTRACT.read_text(encoding="utf-8")
        self.assertIn("standard OpenAI API list-price", contract)
        self.assertIn("best-effort observability", contract)
        self.assertIn("## Cost meaning", contract)
        self.assertIn("## Concurrency correctness boundary", contract)
        ledger = CODEX_LEDGER.read_text(encoding="utf-8")
        self.assertIn("codex_ledger_token_usage_total", ledger)
        self.assertIn("### Bind address", ledger)

    def test_readme_stays_a_landing_page(self) -> None:
        """Internals belong in docs/; a README past this size is unreadable."""
        words = len(README.read_text(encoding="utf-8").split())
        self.assertLessEqual(words, README_MAX_WORDS)
        readme = README.read_text(encoding="utf-8")
        for pointer in (
            "docs/operations.md",
            "docs/codex-ledger.md",
            "docs/metrics-contract.md",
        ):
            self.assertIn(pointer, readme)

    def test_documentation_makes_no_unsupported_windows_claim(self) -> None:
        for path in (
            README,
            TROUBLESHOOTING,
            OPERATIONS,
            CODEX_LEDGER,
            METRICS_CONTRACT,
        ):
            with self.subTest(path=path.name):
                self.assertNotIn(
                    "windows",
                    path.read_text(encoding="utf-8").casefold(),
                )

    def test_compose_has_no_fixed_container_names_or_admin_api(self) -> None:
        content = COMPOSE.read_text(encoding="utf-8")
        self.assertNotIn("container_name:", content)
        self.assertNotIn("--web.enable-admin-api", content)

    def test_compose_variables_have_template_entries(self) -> None:
        compose_variables = set(
            re.findall(r"\$\{([A-Z][A-Z0-9_]*)", COMPOSE.read_text(encoding="utf-8"))
        )
        self.assertEqual(compose_variables - environment_values().keys(), set())

    def test_image_defaults_use_manifest_digests(self) -> None:
        values = environment_values()
        for variable in (
            "GRAFANA_IMAGE",
            "PROMETHEUS_IMAGE",
            "OTEL_COLLECTOR_IMAGE",
        ):
            with self.subTest(variable=variable):
                self.assertRegex(values[variable], r"^[^@\s]+@sha256:[0-9a-f]{64}$")

    def test_template_contains_no_inline_secret(self) -> None:
        """Secret-shaped keys ship blank; ``*_FILE`` keys name an ignored path.

        A ``*_FILE`` value is legitimately non-empty, so emptiness is the wrong
        assertion for it. Assert the property that actually matters instead: the
        file it points at must be ignored by the versioned ``.gitignore``, so a
        secret placed there cannot be committed by anyone who clones this repo.

        Provenance matters. ``git check-ignore`` also consults
        ``.git/info/exclude`` and the user's global excludes, neither of which
        ships. A rule that lives only there passes locally and protects nobody
        downstream, so the matching source is checked, not just the exit code.
        """
        secret_key = re.compile(r"(PASSWORD|TOKEN|SECRET|API_KEY)(_FILE)?$")
        in_checkout = (
            subprocess.run(
                ["git", "rev-parse", "--git-dir"],
                cwd=ROOT,
                check=False,
                capture_output=True,
            ).returncode
            == 0
        )
        for key, value in environment_values().items():
            match = secret_key.search(key)
            if match is None:
                continue
            with self.subTest(key=key):
                if match.group(2) is None:
                    self.assertEqual(value, "")
                    continue
                if not value or value.startswith(("/", "~")):
                    # Blank, or a host path outside the worktree: unable to be
                    # committed either way, so there is nothing to assert.
                    continue
                if not in_checkout:
                    self.skipTest("not a git checkout")
                found = subprocess.run(
                    ["git", "check-ignore", "-v", value],
                    cwd=ROOT,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(
                    found.returncode,
                    0,
                    f"{key} points at {value}, which is not ignored",
                )
                source = found.stdout.split(":", 1)[0]
                self.assertEqual(
                    source,
                    ".gitignore",
                    f"{key} is ignored only by {source}, which does not ship",
                )

    def test_privacy_allowlist_keeps_required_metric_dimensions(self) -> None:
        content = COLLECTOR.read_text(encoding="utf-8")
        match = re.search(
            r'keep_matching_keys\(datapoint\.attributes, "\^\(([^"]+)\)\$"\)',
            content,
        )
        self.assertIsNotNone(match)
        allowlist = set(match.group(1).split("|")) if match else set()
        expected = {
            "agent[.]name",
            "app[.]entrypoint",
            "decision",
            "effort",
            "is_git",
            "model",
            "start_type",
            "token_type",
            "type",
        }
        self.assertEqual(allowlist, expected)


if __name__ == "__main__":
    unittest.main()
