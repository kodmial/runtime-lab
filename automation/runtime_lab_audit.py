"""Static audit helpers for the runtime-lab architecture review (issue #14).

These helpers encode the invariants from issues #1/#4/#10 so drift can be
caught by automated tests without executing any Render/GitHub API calls.

All functions are stdlib-only and operate on file text, so they stay usable
in the minimal CI environment. They do not provision, modify, or delete any
external resource.
"""

from __future__ import annotations

import json
import os
import re

CONTROL_PLANE_ALLOWED_BACKENDS = ("actions", "render-controller")

ALLOWED_RENDER_REGIONS = ("oregon", "ohio", "virginia", "singapore")

PREFERRED_MODEL = "opencode/muse-spark-1.3-contributor-free"
FALLBACK_MODEL = "opencode/space-bunny-free"
ALLOWED_MODELS = (PREFERRED_MODEL, FALLBACK_MODEL)

RENDER_HARNESS_FILES = (
    "automation/render-job.sh",
    "automation/render-cleanup.sh",
)


def load_control_plane(path: str) -> dict:
    """Load and validate automation/control-plane.json."""
    with open(path, "r", encoding="utf-8") as handle:
        config = json.load(handle)
    if not isinstance(config, dict):
        raise ValueError("control-plane.json must contain a JSON object")
    backend = config.get("execution_backend")
    if backend not in CONTROL_PLANE_ALLOWED_BACKENDS:
        raise ValueError(
            "execution_backend must be one of "
            + ", ".join(CONTROL_PLANE_ALLOWED_BACKENDS)
            + "; got %r" % (backend,)
        )
    return config


def is_allowed_region(region: str | None) -> bool:
    """Return True only for Muse-eligible worker regions (US + Singapore)."""
    if not isinstance(region, str):
        return False
    return region.strip().lower() in ALLOWED_RENDER_REGIONS


def is_muse_model(model: str | None) -> bool:
    """Return True when the model is the Muse Spark preferred model."""
    if not isinstance(model, str):
        return False
    return model.strip() == PREFERRED_MODEL


def is_allowed_model(model: str | None) -> bool:
    """Return True for the preferred model or the Space Bunny fallback."""
    if not isinstance(model, str):
        return False
    return model.strip() in ALLOWED_MODELS


def scheduler_wip_default(text: str) -> str | None:
    """Extract the WIP limit default from scheduler text.

    Supports the legacy ``vars.AUTOMATION_WIP_LIMIT || '4'`` form as well
    as the hardcoded ``WIP_LIMIT: '6'`` env form (with the
    ``positiveInt('WIP_LIMIT', 6)`` fallback as secondary evidence).
    """
    match = re.search(
        r"AUTOMATION_WIP_LIMIT\s*\|\|\s*['\"]([^'\"]+)['\"]", text
    )
    if match:
        return match.group(1)
    match = re.search(r"WIP_LIMIT:\s*['\"]([^'\"]+)['\"]", text)
    if match:
        return match.group(1)
    match = re.search(
        r"positiveInt\(\s*['\"]WIP_LIMIT['\"]\s*,\s*(\d+)", text
    )
    return match.group(1) if match else None


def scheduler_max_attempts_default(text: str) -> str | None:
    """Extract the AUTOMATION_MAX_DISPATCH_ATTEMPTS fallback."""
    match = re.search(
        r"AUTOMATION_MAX_DISPATCH_ATTEMPTS\s*\|\|\s*['\"]([^'\"]+)['\"]", text
    )
    return match.group(1) if match else None


def has_per_issue_render_concurrency(text: str) -> bool:
    """Check the Render executor uses a per-issue (not global) mutex."""
    return "group: runtime-lab-render-${{ inputs.issue_number }}" in text


def has_serialized_render_concurrency(text: str) -> bool:
    """Check the Render executor uses the intentional single-service mutex.

    Commit 8624427 serializes ephemeral Render workers globally as
    ``group: runtime-lab-render-single-service`` (only one
    automation-owned worker at a time). This is the current workflow
    intent, so the audit treats it as valid concurrency alongside the
    legacy per-issue group.
    """
    return "group: runtime-lab-render-single-service" in text


def has_valid_render_concurrency(text: str) -> bool:
    """True for either accepted Render concurrency model."""
    return has_per_issue_render_concurrency(
        text
    ) or has_serialized_render_concurrency(text)


def has_global_render_mutex(text: str) -> bool:
    """Detect a global single-job Render mutex (forbidden by #1/#9/#10).

    The intentional ``runtime-lab-render-single-service`` group is
    exempt: it is the current global serialization mechanism, not an
    accidental bare ``runtime-lab-render`` mutex.
    """
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("group: runtime-lab-render") and (
            "inputs.issue_number" not in stripped
        ):
            if "runtime-lab-render-single-service" in stripped:
                continue
            return True
    return False


def has_per_issue_opencode_concurrency(text: str) -> bool:
    """Check the OpenCode workflow serializes per issue but not globally."""
    return "opencode-${{ inputs.issue_number" in text


def references_render_harness_scripts(text: str) -> bool:
    """Check a workflow text references both harness entrypoints from #4."""
    return (
        "automation/render-job.sh" in text
        and "automation/render-cleanup.sh" in text
    )


def render_harness_files_present(repo_root: str) -> dict[str, bool]:
    """Report whether the #4 harness entrypoints exist (no side effects)."""
    result: dict[str, bool] = {}
    for relative in RENDER_HARNESS_FILES:
        result[relative] = os.path.isfile(os.path.join(repo_root, relative))
    return result
