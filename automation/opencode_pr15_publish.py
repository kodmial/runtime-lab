"""Trusted publishing path for the exact OpenCode PR #15 artifact (issue #140).

Stdlib only, no git mutations, no workflow edits, no Render service.
This module is the single production owner for reconstructing the exact
PR #15 binary from the pinned source HEAD, verifying the published
exact fingerprint, staging the immutable Actions payload, capturing the
publish-time transport identity, and handing it to the downstream Render
qualification issue without user/chat intervention.

Exact source (fail closed, never substituted):

- repository ``kodmial/opencode``, PR ``#15``, branch ``coding-no-mini``;
- PR head SHA ``842157c38db9f8178ed0eee7af32f7536fe2346e``;
- observed merge SHA ``0d649350557c5ee3882cc55e5ca65f919ab304c4``
  (context only; the unstamped merge-ref binary is never tested);
- source package / runtime version ``1.18.33``;
- build command
  ``OPENCODE_VERSION=1.18.33 bun run --cwd packages/opencode script/build.ts --coding --single``;
- expected binary SHA-256
  ``d9f930c1e288fc81a4abb12f0dd3974584ab8d28d5587cfd6c979698fe45f0c0``;
- expected binary size ``171218400`` bytes.

Payload (immutable Actions artifact):

- ``opencode-coding-linux-x64``
- ``opencode-coding-linux-x64.sha256``
- ``build-metadata.txt``

Transport identity (captured at publish time, never invented):

- source workflow run id (numeric Actions run);
- artifact id (numeric Actions artifact);
- artifact archive SHA-256 (zip digest);
- binary SHA-256 + version (pinned above).

Delivery (owned by automation/exact_artifact_delivery.py +
automation/render_lifecycle.py, extended data-driven by #140): the
controller downloads with its own credential, verifies archive/binary/
version, pushes verified bytes only (no GitHub credential ever reaches
the worker), launches the deterministic absolute path with zero
fallback, and proves ``/proc/<pid>/exe`` SHA/cmdline. The Docker
``marginal`` verdict from #134 is recorded evidence, never a gate: the
explicit purpose here is the real Render measurement.

Handoff (automatic, no chat): the publishing workflow updates the
coordinator-created downstream Render qualification issue with the
final exact fields BEFORE unpausing it, then explicitly dispatches the
issue scheduler (and the Render executor via the scheduler) so Render
starts on its own.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from typing import Any, Mapping, Sequence

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

import opencode_pr15_qualify as _pr15

SCHEMA = "runtime-lab-pr15-publish/v1"

FORK_REPO = _pr15.FORK_REPO
FORK_PR = _pr15.FORK_PR
FORK_BRANCH = _pr15.FORK_BRANCH
SOURCE_SHA = _pr15.SOURCE_SHA
MERGE_SHA = _pr15.MERGE_SHA
EXPECTED_VERSION = _pr15.EXPECTED_VERSION
BINARY_SHA256 = "d9f930c1e288fc81a4abb12f0dd3974584ab8d28d5587cfd6c979698fe45f0c0"
BINARY_BYTES = 171218400
BUILD_COMMAND = _pr15.BUILD_COMMAND

ARTIFACT_NAME = "opencode-coding-linux-x64"
ARTIFACT_PAYLOAD = (
    "opencode-coding-linux-x64",
    "opencode-coding-linux-x64.sha256",
    "build-metadata.txt",
)
BINARY_FILENAME = "opencode-coding-linux-x64"
CHECKSUM_FILENAME = "opencode-coding-linux-x64.sha256"
METADATA_FILENAME = "build-metadata.txt"

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _is_numeric_id(value: object) -> bool:
    try:
        text = str(value or "").strip()
    except Exception:
        return False
    return text.isdigit() and len(text) >= 5


def validate_publish_source(
    source_sha: str = SOURCE_SHA,
    merge_sha: str = MERGE_SHA,
    branch: str = FORK_BRANCH,
    pr: int = FORK_PR,
) -> dict[str, str]:
    """Fail closed unless the exact PR #15 source is supplied."""
    return _pr15.validate_source_identity(source_sha, merge_sha, branch, pr)


def build_steps_for_publish(workdir: str = "$WORKDIR/opencode-140") -> list[str]:
    """Reproducible shell steps rebuilding the exact PR #15 head (no edits)."""
    return [
        "gh api repos/%s/pulls/%d --jq '{head: .head.sha, merge: .merge_commit_sha}'"
        % (FORK_REPO, FORK_PR),
        "test \"$(gh api repos/%s/pulls/%d --jq '.head.sha')\" = \"%s\""
        % (FORK_REPO, FORK_PR, SOURCE_SHA),
        "download the exact-head tarball "
        "https://api.github.com/repos/%s/tarball/%s "
        "(verify PR #15 file markers after extract; never clone a moving ref)"
        % (FORK_REPO, SOURCE_SHA),
        "bun install --frozen-lockfile  # from the extracted source root",
        BUILD_COMMAND,
        "%s --version  # must print exactly %s" % ("<built-binary>", EXPECTED_VERSION),
        "test \"$(sha256sum <built-binary> | awk '{print $1}')\" = \"%s\"" % BINARY_SHA256,
        "test \"$(stat -c %%s <built-binary>)\" = \"%d\"" % BINARY_BYTES,
        "stage payload %s + %s + %s and upload as one immutable Actions artifact "
        "(actions/upload-artifact; never overwrite)" % ARTIFACT_PAYLOAD,
    ]


def sha256_of_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_built_binary(
    binary_path: str,
    *,
    expected_sha256: str = BINARY_SHA256,
    expected_bytes: int = BINARY_BYTES,
    expected_version: str = EXPECTED_VERSION,
    version_output: str | None = None,
    runner=None,
) -> dict[str, Any]:
    """Fail closed unless the built binary matches the published fingerprint.

    Checks: file exists + executable, size equals ``171218400`` bytes,
    SHA-256 equals ``d9f930c1...45f0c0``, and ``--version`` prints exactly
    ``1.18.33``. ``version_output`` (or ``runner``) is an offline
    injection seam: when ``None`` the real binary is executed with a
    60 s timeout. Never silently tests PR #12/main/latest/installer or
    the unstamped merge-ref binary (``0.0.0--...`` fails the version
    gate first).
    """
    if not isinstance(binary_path, str) or not binary_path:
        raise ValueError("binary_path must be a non-empty string")
    if not os.path.isfile(binary_path):
        raise FileNotFoundError("built binary not found: %r" % binary_path)
    if not os.access(binary_path, os.X_OK):
        raise ValueError("built binary is non-executable: %r" % binary_path)
    actual_bytes = os.path.getsize(binary_path)
    if int(actual_bytes) != int(expected_bytes):
        raise ValueError(
            "built binary size %d != expected %d; never substitute another binary"
            % (actual_bytes, int(expected_bytes))
        )
    actual_sha = sha256_of_file(binary_path)
    if SHA256_RE.match(actual_sha) is None:
        raise ValueError("cannot fingerprint built binary checksum")
    if actual_sha != str(expected_sha256).strip().lower():
        raise ValueError(
            "built binary SHA-256 %s != fingerprinted %s"
            % (actual_sha, str(expected_sha256).strip().lower())
        )
    if version_output is None:
        if runner is not None:
            version_output = runner(binary_path)
        else:
            completed = subprocess.run(
                [binary_path, "--version"],
                timeout=60.0,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            raw = ((completed.stdout or "") + " " + (completed.stderr or "")).strip()
            version_output = raw.split()[0] if raw else ""
    version_text = str(version_output or "").strip()
    if not version_text:
        raise ValueError("binary --version output is empty")
    if version_text != str(expected_version).strip():
        raise ValueError(
            "binary --version %r != expected %r; unstamped merge-ref builds "
            "trip the free-tier minimum-version gate" % (version_text, expected_version)
        )
    return {
        "binary_path": binary_path,
        "binary_sha256": actual_sha,
        "binary_bytes": int(actual_bytes),
        "version": str(expected_version).strip(),
    }


def stage_publish_payload(binary_path: str, out_dir: str) -> dict[str, str]:
    """Stage the immutable upload payload (fail closed).

    Copies the verified binary to
    ``<out_dir>/opencode-coding-linux-x64``, writes the bundled
    ``opencode-coding-linux-x64.sha256`` checksum file plus
    ``build-metadata.txt``, and returns paths + digests. The caller
    uploads the whole directory as ONE immutable Actions artifact with
    ``actions/upload-artifact`` (never overwrite/re-upload under the
    same name).
    """
    if not isinstance(out_dir, str) or not out_dir.strip():
        raise ValueError("out_dir must be a non-empty string")
    verified = verify_built_binary(binary_path)
    os.makedirs(out_dir, exist_ok=True)
    dest_binary = os.path.join(out_dir, BINARY_FILENAME)
    shutil.copyfile(binary_path, dest_binary)
    os.chmod(dest_binary, 0o755)
    if sha256_of_file(dest_binary) != verified["binary_sha256"]:
        raise ValueError("staged binary checksum mismatch after copy")
    checksum_path = os.path.join(out_dir, CHECKSUM_FILENAME)
    with open(checksum_path, "w", encoding="utf-8") as handle:
        handle.write("%s  %s\n" % (verified["binary_sha256"], BINARY_FILENAME))
    metadata_path = os.path.join(out_dir, METADATA_FILENAME)
    with open(metadata_path, "w", encoding="utf-8") as handle:
        handle.write(
            "opencode_version=%s\n"
            "source_repo=%s\n"
            "source_pr=%d\n"
            "source_branch=%s\n"
            "source_sha=%s\n"
            "merge_sha=%s\n"
            "build_command=%s\n"
            "binary_sha256=%s\n"
            "binary_bytes=%d\n"
            % (
                EXPECTED_VERSION,
                FORK_REPO,
                FORK_PR,
                FORK_BRANCH,
                SOURCE_SHA,
                MERGE_SHA,
                BUILD_COMMAND,
                verified["binary_sha256"],
                verified["binary_bytes"],
            )
        )
    names = sorted(os.listdir(out_dir))
    missing = [name for name in ARTIFACT_PAYLOAD if name not in names]
    if missing:
        raise ValueError("staged payload missing files: %s" % sorted(missing))
    return {
        "out_dir": os.path.abspath(out_dir),
        "binary": dest_binary,
        "checksum": checksum_path,
        "metadata": metadata_path,
        "binary_sha256": verified["binary_sha256"],
        "binary_bytes": str(verified["binary_bytes"]),
        "version": verified["version"],
    }


def build_publish_manifest(
    *,
    source_run_id: str,
    artifact_id: str,
    archive_sha256: str,
    binary_sha256: str = BINARY_SHA256,
    version: str = EXPECTED_VERSION,
) -> dict[str, str]:
    """Capture the final immutable transport identity (fail closed).

    All publish-time fields (numeric run/artifact ids, 64-hex archive
    digest) are validated; the binary/version must equal the published
    PR #15 fingerprint. The returned manifest is what the publishing workflow
    writes into the downstream Render issue before unpausing it.
    """
    run = str(source_run_id or "").strip()
    artifact = str(artifact_id or "").strip()
    archive = str(archive_sha256 or "").strip().lower()
    binary = str(binary_sha256 or "").strip().lower()
    ver = str(version or "").strip()
    if not _is_numeric_id(run):
        raise ValueError("source_run_id must be a numeric Actions run id")
    if not _is_numeric_id(artifact):
        raise ValueError("artifact_id must be a numeric Actions artifact id")
    if SHA256_RE.match(archive) is None:
        raise ValueError("archive_sha256 must be 64 lowercase hex chars")
    if binary != BINARY_SHA256:
        raise ValueError("binary_sha256 must equal the published PR #15 fingerprint %s" % BINARY_SHA256)
    if ver != EXPECTED_VERSION:
        raise ValueError("version must equal %r" % EXPECTED_VERSION)
    try:
        from automation.exact_artifact_delivery import (  # type: ignore[import-not-found]
            build_pr15_artifact_identity as _build_pr15,
        )
    except ImportError:
        try:
            from exact_artifact_delivery import (  # type: ignore[no-redef]
                build_pr15_artifact_identity as _build_pr15,
            )
        except ImportError:
            _build_pr15 = None  # type: ignore[assignment]
    if _build_pr15 is not None:
        identity = dict(_build_pr15(artifact, run, archive))
    else:
        identity = {
            "artifact_id": artifact,
            "artifact_name": ARTIFACT_NAME,
            "source_run_id": run,
            "archive_sha256": archive,
            "binary_sha256": BINARY_SHA256,
            "version": EXPECTED_VERSION,
            "source_sha": SOURCE_SHA,
            "merge_sha": MERGE_SHA,
            "repo": FORK_REPO,
            "pr": str(FORK_PR),
            "branch": FORK_BRANCH,
        }
    manifest = {
        "schema": SCHEMA,
        "repo": FORK_REPO,
        "pr": str(FORK_PR),
        "branch": FORK_BRANCH,
        "source_sha": SOURCE_SHA,
        "merge_sha": MERGE_SHA,
        "build_command": BUILD_COMMAND,
        "artifact_name": ARTIFACT_NAME,
        "source_run_id": run,
        "artifact_id": artifact,
        "archive_sha256": archive,
        "binary_sha256": BINARY_SHA256,
        "binary_bytes": str(BINARY_BYTES),
        "version": EXPECTED_VERSION,
    }
    manifest.update({"identity_%s" % k: v for k, v in identity.items()})
    return manifest


def render_downstream_issue_body(manifest: Mapping[str, str]) -> str:
    """Render the downstream Render qualification issue body (exact fields).

    The body carries every field BOTH parsers need: the
    ``docker-qualification.yml`` ``value('<Label>')`` labels
    (``Artifact ID``, ``Source workflow run``, ``Source/head SHA``,
    ``Artifact archive digest``, ``Expected binary SHA-256``,
    ``Expected --version``) and the ``render_lifecycle`` gate phrasings
    (``artifact ID``, ``workflow run``, ``sha256:<digest>``,
    ``binary SHA-256``, ``artifact name``). The Docker ``marginal``
    verdict from #134 is cited as evidence only, never as a gate.
    """
    get = lambda key: str(manifest.get(key, "") or "").strip()
    artifact_id = get("artifact_id")
    run = get("source_run_id")
    archive = get("archive_sha256").lower()
    binary = get("binary_sha256").lower()
    version = get("version") or EXPECTED_VERSION
    if not _is_numeric_id(artifact_id) or not _is_numeric_id(run):
        raise ValueError("manifest transport ids must be numeric")
    if SHA256_RE.match(archive) is None or SHA256_RE.match(binary) is None:
        raise ValueError("manifest digests must be 64 hex chars")
    if binary != BINARY_SHA256 or version != EXPECTED_VERSION:
        raise ValueError("manifest binary/version must equal the published PR #15 fingerprint")
    lines = [
        "<!-- runtime-lab-qualification-managed -->",
        "<!-- runtime-lab-pr15-render-qualification -->",
        "## Render qualification: exact OpenCode PR #15 artifact",
        "",
        "P0 — run the already-measured PR #15 binary on a real Render Free",
        "512 MiB worker through the real exact-artifact path. Docker-only",
        "evidence is not sufficient. Do NOT gate this candidate out because",
        "#134 called Docker marginal: the explicit purpose is the real",
        "Render measurement.",
        "",
        "## Exact immutable artifact",
        "",
        "- Repository: `%s`" % FORK_REPO,
        "- PR: `#%d`" % FORK_PR,
        "- Branch: `%s`" % FORK_BRANCH,
        "- Source/head SHA: `%s`" % SOURCE_SHA,
        "- PR merge SHA (context only): `%s`" % MERGE_SHA,
        "- Source workflow run: `%s`" % run,
        "- Artifact name: `%s`" % ARTIFACT_NAME,
        "- Artifact ID: `%s`" % artifact_id,
        "- Artifact archive digest: `sha256:%s`" % archive,
        "- Expected binary SHA-256: `%s`" % binary,
        "- binary SHA-256: `%s`" % binary,
        "- Expected --version: `%s`" % version,
        "- Expected binary bytes: `%d`" % BINARY_BYTES,
        "- Build command: `%s`" % BUILD_COMMAND,
        "",
        "Download exact artifact ID `%s` from source run `%s`." % (artifact_id, run),
        "Verify archive `sha256:%s` before extraction/use with no rebuild" % archive,
        "and no binary substitution. The binary must report exactly `%s`" % version,
        "and SHA-256 `%s`." % binary,
        "",
        "## Coding workload",
        "",
        "Run a real autonomous coding task requiring repository",
        "inspection/search, file reasoning, at least one real edit,",
        "shell/build/test execution, and deterministic verification.",
        "A version smoke test, hello-world prompt, documentation-only run,",
        "or fake harness does not count.",
        "",
        "## Render invariants",
        "",
        "- Exactly one automation-owned Render service may exist at a time globally.",
        "- Free tier only; no paid resource.",
        "- One service per attempt; no second service for retry/fallback.",
        "- Controller-side credentialed fetch; worker receives no GitHub credential.",
        "- Verify archive SHA and binary SHA before launch.",
        "- Launch exact binary by deterministic absolute path with no",
        "  baseline/PATH/HOME/installer fallback.",
        "- Record downloaded SHA separately from /proc/<pid>/exe realpath +",
        "  SHA-256 + cmdline.",
        "- Cleanup runs on every terminal path and success requires verified",
        "  service absence.",
        "",
        "## Prior evidence (not a gate)",
        "",
        "- Docker 512 MiB/no-swap verdict from #134 is marginal (throttled",
        "  passes, `max` stall events, zero `oom_kill`); it motivates this",
        "  Render run and must not block it.",
        "",
        "## Result contract",
        "",
        "Publish one machine-readable result marker for this exact artifact.",
        "Distinguish infrastructure, memory, correctness/capability and",
        "identity failures.",
    ]
    return "\n".join(lines) + "\n"


def build_handoff_plan(
    manifest: Mapping[str, str],
    downstream_issue: int,
    *,
    execution_label: str = "execution:render-e2e",
    mode: str = "e2e",
) -> dict[str, Any]:
    """Build the automatic handoff plan (no chat, no manual step).

    Returns ``{"issue_body": ..., "commands": [...], "dispatches": [...]}``.
    The publishing workflow rewrites the downstream issue first, adds the
    execution/qualification labels and ``automation:in-progress`` while the
    issue is still paused, then removes ``automation:paused`` and dispatches
    exactly one ``render-executor.yml`` run. This ordering prevents the
    issue scheduler from racing the direct dispatch and creating a duplicate
    Render run. The plan never gates on the #134 marginal verdict.
    """
    try:
        number = int(downstream_issue)
    except (TypeError, ValueError) as exc:
        raise ValueError("downstream_issue must be a positive integer") from exc
    if number <= 0:
        raise ValueError("downstream_issue must be a positive integer")
    if execution_label not in ("execution:render-smoke", "execution:render-e2e"):
        raise ValueError("execution_label must be a Render execution label")
    if mode not in ("smoke", "e2e"):
        raise ValueError("mode must be smoke or e2e")
    body = render_downstream_issue_body(manifest)
    get = lambda key: str(manifest.get(key, "") or "").strip()
    commands: list[list[str]] = [
        ["gh", "issue", "view", str(number), "--json", "number,title,body"],
        [
            "gh", "issue", "edit", str(number),
            "--body-file", "<render_downstream_issue_body>",
        ],
        ["gh", "issue", "edit", str(number), "--add-label", execution_label],
        ["gh", "issue", "edit", str(number), "--add-label", "qualification:render"],
        ["gh", "issue", "edit", str(number), "--add-label", "automation:in-progress"],
        ["gh", "issue", "edit", str(number), "--remove-label", "automation:paused"],
        [
            "gh", "workflow", "run", "render-executor.yml", "--ref", "main",
            "-f", "issue_number=%d" % number, "-f", "mode=%s" % mode,
        ],
    ]
    return {
        "schema": SCHEMA,
        "downstream_issue": number,
        "execution_label": execution_label,
        "mode": mode,
        "artifact_id": get("artifact_id"),
        "source_run_id": get("source_run_id"),
        "archive_sha256": get("archive_sha256"),
        "binary_sha256": get("binary_sha256"),
        "version": get("version") or EXPECTED_VERSION,
        "issue_body": body,
        "commands": commands,
        "dispatches": [
            {
                "workflow": "render-executor.yml",
                "ref": "main",
                "inputs": {"issue_number": str(number), "mode": mode},
            },
        ],
    }


def run_handoff_plan(plan: Mapping[str, Any], *, dry_run: bool = False) -> list[str]:
    """Execute a handoff plan (publishing workflow side, credentialed).

    Writes the rendered issue body to a temp file for the
    ``--body-file`` step, runs every ``gh`` command in order, and
    returns the executed argv strings. ``dry_run=True`` returns the
    argv strings without executing (used by tests/offline validation).
    Never prints credential values; failures raise (fail closed before
    unpausing when the body/identity is wrong).
    """
    if not isinstance(plan, Mapping):
        raise ValueError("plan must be a mapping")
    commands = plan.get("commands")
    if not isinstance(commands, Sequence) or not commands:
        raise ValueError("plan carries no commands")
    body = str(plan.get("issue_body", "") or "")
    if "Artifact ID" not in body or "Source workflow run" not in body:
        raise ValueError("plan issue_body misses the exact artifact fields")
    rendered: list[str] = []
    body_file = ""
    if "<render_downstream_issue_body>" in json.dumps(commands):
        import tempfile

        fd, body_file = tempfile.mkstemp(prefix="pr15-issue-", suffix=".md")
        with open(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
    try:
        for argv in commands:
            if not isinstance(argv, Sequence) or not argv:
                raise ValueError("handoff command must be a non-empty argv list")
            resolved = [
                body_file if str(part) == "<render_downstream_issue_body>" else str(part)
                for part in argv
            ]
            rendered.append(" ".join(resolved))
            if dry_run:
                continue
            completed = subprocess.run(resolved, timeout=120.0)
            if completed.returncode != 0:
                raise RuntimeError(
                    "handoff command failed (%d): %s"
                    % (completed.returncode, " ".join(resolved[:4]))
                )
    finally:
        if body_file:
            try:
                os.unlink(body_file)
            except OSError:
                pass
    return rendered


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Publish/verify the exact PR #15 artifact and hand off to Render."
    )
    parser.add_argument("--binary", default="", help="Built binary to verify/stage")
    parser.add_argument("--out-dir", default="", help="Payload staging directory")
    parser.add_argument("--source-run-id", default="")
    parser.add_argument("--artifact-id", default="")
    parser.add_argument("--archive-sha256", default="")
    parser.add_argument("--manifest-out", default="")
    parser.add_argument("--downstream-issue", default="")
    parser.add_argument("--mode", default="e2e", choices=["smoke", "e2e"])
    parser.add_argument(
        "--execution-label",
        default="execution:render-e2e",
        choices=["execution:render-smoke", "execution:render-e2e"],
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--print-steps", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.print_steps:
        for step in build_steps_for_publish():
            print(step)
    manifest: dict[str, str] | None = None
    if args.binary:
        if args.out_dir:
            staged = stage_publish_payload(args.binary, args.out_dir)
            print(json.dumps(staged, sort_keys=True, indent=2))
        else:
            verified = verify_built_binary(args.binary)
            print(json.dumps(verified, sort_keys=True, indent=2))
    if args.source_run_id or args.artifact_id or args.archive_sha256:
        manifest = build_publish_manifest(
            source_run_id=args.source_run_id,
            artifact_id=args.artifact_id,
            archive_sha256=args.archive_sha256,
        )
        print(json.dumps(manifest, sort_keys=True, indent=2))
        if args.manifest_out:
            with open(args.manifest_out, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    if args.downstream_issue:
        if manifest is None:
            raise SystemExit(
                "error: --downstream-issue requires --source-run-id/--artifact-id/--archive-sha256"
            )
        plan = build_handoff_plan(
            manifest,
            int(args.downstream_issue),
            execution_label=args.execution_label,
            mode=args.mode,
        )
        print(plan["issue_body"])
        rendered = run_handoff_plan(plan, dry_run=args.dry_run)
        for line in rendered:
            print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
