# SPDX-License-Identifier: Apache-2.0
"""Regression guards for credential / PII leakage in the repo.

The kahzaabu project is published OSS under Apache-2.0. A leaked API
key or hardcoded developer-machine path would be a real reputational
problem. These tests pin the security audit's findings: no live-shape
secrets in any tracked file, no hardcoded `/Users/<name>/...` paths
in active code.

If you ever need to add a placeholder credential to documentation
(e.g. `sk-ant-PLACEHOLDER` in a setup example), keep it under 30
characters so the entropy-based patterns below don't fire.
"""
from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


# Things shaped like live credentials. Each pattern requires enough
# length that a placeholder won't accidentally trip it.
SECRET_PATTERNS: list[tuple[str, str]] = [
    (r"sk-ant-[A-Za-z0-9_-]{40,}",      "Anthropic API key"),
    (r"sk-[A-Za-z0-9]{40,}",            "OpenAI-style API key"),
    (r"pa-[A-Za-z0-9_-]{30,}",          "Voyage API key"),
    (r"AKIA[A-Z0-9]{16}",               "AWS access key ID"),
    (r"AIza[A-Za-z0-9_-]{35}",          "Google API key"),
    (r"ghp_[A-Za-z0-9]{36}",            "GitHub PAT"),
    (r"xox[abprs]-[A-Za-z0-9-]{20,}",   "Slack token"),
]

# Excluded paths — git-internal, venvs, node_modules, etc.
EXCLUDE_PARTS = {".git", "node_modules", ".venv", ".venv-mcp",
                  "__pycache__", "dist", "build", ".idea", ".DS_Store"}


def _walk_repo_files():
    """Yield every tracked-or-modified file we should scan. We use
    git ls-files so untracked junk (e.g. local .env experiments) is
    not flagged."""
    out = subprocess.check_output(
        ["git", "-C", str(ROOT), "ls-files"], text=True)
    for ln in out.splitlines():
        if not ln.strip(): continue
        p = ROOT / ln
        if not p.is_file(): continue
        if any(part in EXCLUDE_PARTS for part in p.parts): continue
        yield p


class SecretShapeGuardTests(unittest.TestCase):
    def test_no_live_shape_credentials_in_tracked_files(self):
        offenders: list[str] = []
        for p in _walk_repo_files():
            try:
                text = p.read_text()
            except (UnicodeDecodeError, FileNotFoundError):
                continue
            for pat, label in SECRET_PATTERNS:
                for m in re.finditer(pat, text):
                    rel = p.relative_to(ROOT)
                    offenders.append(f"{rel}: {label} match {m.group(0)[:30]}…")
        self.assertEqual(offenders, [],
            "Live-shape credential pattern found in tracked file. "
            "If this is a placeholder, shorten it to <30 chars. If "
            "it's real, ROTATE THE KEY IMMEDIATELY and `git filter-"
            "branch` it out of history. Offenders:\n  "
            + "\n  ".join(offenders))


class DeveloperPathGuardTests(unittest.TestCase):
    """No hardcoded `/Users/<name>/...` or `/home/<name>/...` paths in
    active code. They reveal the developer's machine layout and break
    on any other host."""

    # Patterns that match a hardcoded absolute home-dir path. Generic
    # mentions in docs (e.g. tutorials saying "your /Users/<you>/...")
    # are excluded by requiring at least two path segments after the
    # username.
    HOME_PATH_PATTERNS = [
        re.compile(r"['\"]/Users/[a-zA-Z][a-zA-Z0-9_-]+/[a-zA-Z]"),
        re.compile(r"['\"]/home/[a-zA-Z][a-zA-Z0-9_-]+/[a-zA-Z]"),
    ]

    # Paths under these directories are PERMITTED to mention developer
    # paths:
    #   - tests/    (test fixtures may pin a path)
    #   - scripts/js-verify/  (Node script in a separate scope)
    #   - any *.json fixture file (they're DATA, not code)
    ALLOWED_DIR_PREFIXES = ("tests/", "scripts/js-verify/", "legacy/")

    def test_no_hardcoded_developer_paths_in_active_code(self):
        offenders: list[str] = []
        for p in _walk_repo_files():
            rel = str(p.relative_to(ROOT))
            if any(rel.startswith(prefix)
                   for prefix in self.ALLOWED_DIR_PREFIXES):
                continue
            if p.suffix in (".pdf", ".db", ".bin", ".gz"):
                continue
            try:
                text = p.read_text()
            except (UnicodeDecodeError, FileNotFoundError):
                continue
            for pat in self.HOME_PATH_PATTERNS:
                for m in pat.finditer(text):
                    offenders.append(f"{rel}: {m.group(0)}")
        self.assertEqual(offenders, [],
            "Hardcoded developer home-directory path found. Derive "
            "the path from $0 / __file__ / an env var instead. "
            "Offenders:\n  " + "\n  ".join(offenders))


class DBNotCommittedTests(unittest.TestCase):
    """data/kahzaabu.db contains the bcrypt admin password hash + the
    entire fact-check corpus. It MUST NOT be tracked by git."""

    def test_kahzaabu_db_is_gitignored(self):
        result = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "data/kahzaabu.db"],
            capture_output=True, text=True)
        self.assertEqual(result.stdout.strip(), "",
            "data/kahzaabu.db is tracked by git. It contains bcrypt "
            "password hashes + the full corpus and must stay local-"
            "only. Run `git rm --cached data/kahzaabu.db` to untrack.")

    def test_env_files_gitignored(self):
        for env_name in (".env", ".env.local", ".env.production"):
            result = subprocess.run(
                ["git", "-C", str(ROOT), "check-ignore", "-q", env_name],
                capture_output=True)
            # check-ignore returns 0 if path IS ignored, 1 if not, 128 on error.
            self.assertEqual(result.returncode, 0,
                f"{env_name} would be committed if you `git add` it. "
                "Add to .gitignore.")


if __name__ == "__main__":
    unittest.main(verbosity=2)
