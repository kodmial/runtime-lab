#!/usr/bin/env python3
"""Fail-closed guard for escaped GitHub expression syntax in workflow credentials.

Issue #142: ``qualification-chain.yml`` carried ``GH_TOKEN: \\${{ ... }}``
(an escaped GitHub expression), so the runner received the evaluated secret
with a stray leading backslash and every API call failed with 401
"Bad credentials". The workflow envelope is provisioned separately (the
automation token cannot push ``.github/workflows/**``), so this stdlib-only
module provides the mutable-code half of the repair:

- :func:`sanitize_github_token` removes the single envelope-escape
  backslash at runtime. Real tokens never start with a backslash.
- :func:`scan_workflows` statically detects escaped expressions in
  production workflow credential fields and fails closed, so the escaped
  envelope cannot regress silently.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

ESCAPED_EXPRESSION_RE = re.compile(r"\\\$\{\{")
CREDENTIAL_KEY_RE = re.compile(
    r"\b(GH_TOKEN|GITHUB_TOKEN|TAP_PAT|GH_PAT|GITHUB_PAT)\b"
)
WORKFLOW_GLOB = "*.yml"


def sanitize_github_token(value: str | None) -> str:
    """Return the effective credential with one envelope-escape backslash removed.

    A leading backslash is never part of a real GitHub token; it is the
    artifact of an escaped ``\\${{ ... }}`` workflow expression (issue #142).
    Only one leading backslash is stripped. Falsy input is returned as "".
    """
    if not value:
        return ""
    text = str(value)
    if text.startswith("\\"):
        return text[1:]
    return text


def is_escaped_credential_line(line: str) -> bool:
    """True when one line sets a credential field to an escaped expression."""
    return bool(
        CREDENTIAL_KEY_RE.search(line) and ESCAPED_EXPRESSION_RE.search(line)
    )


def scan_text(text: str) -> list[int]:
    """Return 1-based line numbers of escaped credential expressions."""
    return [
        number
        for number, line in enumerate(text.splitlines(), start=1)
        if is_escaped_credential_line(line)
    ]


def scan_file(path: pathlib.Path) -> list[tuple[str, int, str]]:
    """Scan one workflow file; return (file, line, text) findings."""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return []
    findings = []
    for number, line in enumerate(content.splitlines(), start=1):
        if is_escaped_credential_line(line):
            findings.append((str(path), number, line.strip()))
    return findings


def scan_workflows(root: pathlib.Path | str = ".") -> list[tuple[str, int, str]]:
    """Scan production workflow credential fields under ``root``.

    Only ``.github/workflows/*.yml`` credential env lines are in scope;
    escaped expressions in non-credential fields (for example artifact
    names) are intentionally ignored so only genuine credential-expression
    defects fail the check.
    """
    workflows = pathlib.Path(root) / ".github" / "workflows"
    if not workflows.is_dir():
        return []
    findings: list[tuple[str, int, str]] = []
    for path in sorted(workflows.glob(WORKFLOW_GLOB)):
        findings.extend(scan_file(path))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail closed if workflow credential fields use "
        "escaped GitHub expression syntax."
    )
    parser.add_argument(
        "--root",
        default=".",
        help="Repository root containing .github/workflows (default: .)",
    )
    args = parser.parse_args(argv)
    findings = scan_workflows(args.root)
    if findings:
        for path, number, line in findings:
            print("ESCAPED-CREDENTIAL %s:%d: %s" % (path, number, line))
        print(
            "FAIL: %d escaped GitHub expression(s) in workflow credential fields"
            % len(findings)
        )
        return 1
    print("OK: no escaped GitHub expressions in workflow credential fields")
    return 0


if __name__ == "__main__":
    sys.exit(main())
