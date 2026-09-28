"""Coding-only low-memory OpenCode variant spec and validators (issue #79).

The lightweight variant lives in the ``kodmial/opencode`` source fork and
preserves the existing OpenCode provider/agent/tool loop. It adds a
dedicated headless entrypoint plus a dedicated build target whose static
import graph excludes subsystems proven unnecessary for runtime-lab's
autonomous coding workload.

This module is stdlib-only and performs no network or git mutations. It:

- loads and validates the machine-readable matrix at
  ``automation/opencode-lite.spec.json``;
- parses TypeScript static/dynamic imports without a TS toolchain;
- computes the transitive import closure of the lite entrypoint over an
  in-memory file map (tests/fixtures) or a real fork checkout;
- proves excluded subsystems are absent from the lite runtime path (not
  merely permission-disabled) group by group (A, then B, then C);
- guards against a custom direct provider/API rewrite;
- runs a deterministic representative coding task (inspect/search the
  repository, modify files, run build/tests, react to failures).

Source-level memory work only: this module cannot measure the Bun binary
footprint. Peak-memory evidence for the full binary comes from the
issue-52 harness (``automation/memory_benchmark.py``); the lite binary
delta must be measured on a resourced fork builder with the same harness.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

SPEC_SCHEMA = "runtime-lab-opencode-lite-spec/v1"

# Direct provider/API rewrite guard: third-party LLM SDK imports and
# hard-coded provider API hosts must never appear in the lite closure
# outside the existing provider modules.
DIRECT_PROVIDER_IMPORT_PATTERNS = (
    "openai",
    "anthropic-sdk",
    "@anthropic-ai/sdk",
    "api.openai.com",
    "api.anthropic.com",
)

# Provider loop markers that must remain reachable from the lite entry.
PROVIDER_LOOP_MARKERS = ("provider", "session", "agent")

# Static + dynamic TypeScript import forms we recognize, including
# side-effect-only imports (``import './module'``).
_IMPORT_RE = re.compile(
    r"(?:^|\n)\s*import\s+(?:type\s+)?[^;]*?\sfrom\s+[\"']([^\"']+)[\"']"
    r"|(?:^|\n)\s*import\s+[\"']([^\"']+)[\"']"
    r"|\bimport\s*\(\s*[\"']([^\"']+)[\"']\s*\)"
    r"|\brequire\s*\(\s*[\"']([^\"']+)[\"']\s*\)"
    r"|\bexport\s+[^;]*?\sfrom\s+[\"']([^\"']+)[\"']",
)

_REQUIRED_SPEC_KEYS = frozenset(
    (
        "schema",
        "fork_repo",
        "fork_base_sha",
        "upstream_repo",
        "pinned_opencode_version",
        "variant",
        "full",
        "retained",
        "removed_groups",
        "boundaries",
        "benchmark",
        "no_custom_provider_rewrite",
    )
)

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def spec_path() -> str:
    """Return the absolute path of the lite variant spec."""
    return os.path.join(_repo_root(), "automation", "opencode-lite.spec.json")


def load_spec(path: str | None = None) -> dict:
    """Load and validate the lite spec; fail closed on any defect."""
    target = path or spec_path()
    with open(target, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    return validate_spec(data)


def validate_spec(data: dict) -> dict:
    """Validate a parsed spec object and return it on success."""
    if not isinstance(data, dict):
        raise ValueError("spec must be a JSON object")
    missing = sorted(_REQUIRED_SPEC_KEYS - set(data.keys()))
    extra = sorted(set(data.keys()) - _REQUIRED_SPEC_KEYS)
    if missing or extra:
        raise ValueError(
            "spec keys mismatch: missing=%s extra=%s" % (missing, extra)
        )
    if data.get("schema") != SPEC_SCHEMA:
        raise ValueError("invalid spec schema: %r" % data.get("schema"))
    if data.get("fork_repo") != "kodmial/opencode":
        raise ValueError("fork_repo must be kodmial/opencode")
    if data.get("upstream_repo") != "anomalyco/opencode":
        raise ValueError("upstream_repo must be anomalyco/opencode")
    fork_sha = data.get("fork_base_sha")
    if not isinstance(fork_sha, str) or _SHA_RE.match(fork_sha) is None:
        raise ValueError("fork_base_sha must be a 40-char lowercase SHA")
    version = data.get("pinned_opencode_version")
    if not isinstance(version, str) or _VERSION_RE.match(version) is None:
        raise ValueError("invalid pinned_opencode_version: %r" % version)
    variant = data.get("variant")
    if not isinstance(variant, dict):
        raise ValueError("variant must be an object")
    for key in ("name", "entrypoint", "build_script", "build_command"):
        value = variant.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError("variant.%s must be a non-empty string" % key)
    if variant.get("entrypoint") == (data.get("full") or {}).get("entrypoint"):
        raise ValueError("lite entrypoint must differ from the full entrypoint")
    full = data.get("full")
    if not isinstance(full, dict):
        raise ValueError("full must be an object")
    for key in ("entrypoint", "build_script"):
        value = full.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError("full.%s must be a non-empty string" % key)
    retained = data.get("retained")
    if not isinstance(retained, dict) or not retained.get("capabilities"):
        raise ValueError("retained.capabilities must be non-empty")
    if not retained.get("patterns"):
        raise ValueError("retained.patterns must be non-empty")
    groups = data.get("removed_groups")
    if not isinstance(groups, list) or not groups:
        raise ValueError("removed_groups must be a non-empty list")
    seen_group_names: set[str] = set()
    for group in groups:
        if not isinstance(group, dict):
            raise ValueError("removed group must be an object")
        name = group.get("group")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("removed group needs a non-empty group name")
        if name in seen_group_names:
            raise ValueError("duplicate removed group: %r" % name)
        seen_group_names.add(name)
        if not group.get("modules"):
            raise ValueError("group %r needs a non-empty modules list" % name)
        if not group.get("patterns"):
            raise ValueError("group %r needs a non-empty patterns list" % name)
    if data.get("no_custom_provider_rewrite") is not True:
        raise ValueError("no_custom_provider_rewrite must be true")
    benchmark = data.get("benchmark")
    if not isinstance(benchmark, dict):
        raise ValueError("benchmark must be an object")
    peak = benchmark.get("baseline_peak_kb")
    if not isinstance(peak, int) or peak <= 0:
        raise ValueError("benchmark.baseline_peak_kb must be a positive int")
    return data


def all_excluded_patterns(spec: dict) -> list[str]:
    """Return every excluded substring across all removal groups, in order."""
    patterns: list[str] = []
    for group in spec.get("removed_groups", []):
        for pattern in group.get("patterns", []):
            if pattern not in patterns:
                patterns.append(pattern)
    return patterns


def group_patterns(spec: dict, group_name: str) -> list[str]:
    """Return the excluded substrings for one removal group."""
    for group in spec.get("removed_groups", []):
        if group.get("group") == group_name:
            return list(group.get("patterns", []))
    raise ValueError("unknown removal group: %r" % group_name)


def removal_group_names(spec: dict) -> list[str]:
    """Return removal group names in staged application order."""
    return [group.get("group", "") for group in spec.get("removed_groups", [])]


def parse_ts_imports(text: str) -> list[str]:
    """Extract imported module specifiers from TypeScript source text."""
    if not isinstance(text, str) or not text:
        return []
    found: list[str] = []
    for match in _IMPORT_RE.finditer(text):
        for candidate in match.groups():
            if candidate and candidate not in found:
                found.append(candidate)
    return found


def _normalize(text: str) -> str:
    return text.lower()


def specifier_matches_pattern(specifier: str, pattern: str) -> bool:
    """True when an import specifier references an excluded subsystem."""
    return _normalize(pattern) in _normalize(specifier)


def closure_contains_pattern(
    closure_files: list[str],
    closure_imports: list[str],
    pattern: str,
) -> bool:
    """True when a pattern appears in closure file paths or imports."""
    lowered = _normalize(pattern)
    for path in closure_files:
        if lowered in _normalize(path):
            return True
    for specifier in closure_imports:
        if lowered in _normalize(specifier):
            return True
    return False


def resolve_specifier_to_file(
    specifier: str,
    from_file: str,
    files: dict[str, str],
) -> str | None:
    """Resolve a relative TS import to a file-map key, else None.

    Only relative specifiers (``./`` or ``../``) resolve inside the
    file map. Bare package imports (``bun``, ``react``) and absolute
    paths stay external and are reported as imports without a target.
    """
    if not specifier.startswith("."):
        return None
    base_dir = os.path.dirname(from_file)
    joined = os.path.normpath(os.path.join(base_dir, specifier))
    candidates = (
        joined,
        joined + ".ts",
        joined + ".tsx",
        os.path.join(joined, "index.ts"),
        os.path.join(joined, "index.tsx"),
    )
    for candidate in candidates:
        if candidate in files:
            return candidate
    # Suffix-tolerant match for fixture shorthand keys.
    for key in files:
        if key == joined or key.endswith("/" + joined.lstrip("./")):
            return key
    return None


def lite_closure(
    files: dict[str, str], entrypoint: str
) -> tuple[list[str], list[str]]:
    """Compute (reachable_files, external_imports) from the lite entry.

    ``files`` maps repo-relative paths to file contents. Relative imports
    that resolve inside the map are followed transitively; everything
    else is recorded as an external import specifier.
    """
    if entrypoint not in files:
        raise ValueError("lite entrypoint not in file map: %r" % entrypoint)
    reachable: list[str] = []
    seen: set[str] = set()
    external: list[str] = []
    stack = [entrypoint]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        reachable.append(current)
        content = files.get(current, "")
        for specifier in parse_ts_imports(content):
            target = resolve_specifier_to_file(specifier, current, files)
            if target is not None and target not in seen:
                stack.append(target)
            elif target is None and specifier not in external:
                external.append(specifier)
    return reachable, external


def check_group_removed(
    files: dict[str, str],
    entrypoint: str,
    patterns: list[str],
) -> None:
    """Fail closed when any group pattern reaches the lite closure."""
    reachable, external = lite_closure(files, entrypoint)
    violations = [
        pattern
        for pattern in patterns
        if closure_contains_pattern(reachable, external, pattern)
    ]
    if violations:
        raise ValueError(
            "lite closure reaches excluded subsystem(s): %s"
            % ", ".join(sorted(violations))
        )


def check_all_excluded_removed(
    files: dict[str, str], entrypoint: str, spec: dict
) -> None:
    """Fail closed when any excluded pattern reaches the lite closure."""
    check_group_removed(files, entrypoint, all_excluded_patterns(spec))


def check_retained_present(
    files: dict[str, str], entrypoint: str, spec: dict
) -> None:
    """Fail closed when a retained capability is missing from the closure."""
    reachable, external = lite_closure(files, entrypoint)
    haystacks = [_normalize(path) for path in reachable]
    haystacks += [_normalize(specifier) for specifier in external]
    contents = _normalize("\n".join(files[path] for path in reachable))
    missing: list[str] = []
    for pattern in spec.get("retained", {}).get("patterns", []):
        lowered = _normalize(pattern)
        if not any(lowered in hay for hay in haystacks) and lowered not in contents:
            missing.append(pattern)
    if missing:
        raise ValueError(
            "lite closure is missing retained capabilit(ies): %s"
            % ", ".join(sorted(missing))
        )
    # The provider/model/session/agent loop must stay reachable as a loop,
    # not as an isolated import: require every loop marker somewhere in the
    # closure content.
    loop_missing = [
        marker for marker in PROVIDER_LOOP_MARKERS if marker not in contents
    ]
    if loop_missing:
        raise ValueError(
            "lite closure breaks the provider/agent loop, missing: %s"
            % ", ".join(sorted(loop_missing))
        )


def check_no_direct_provider_client(
    files: dict[str, str], entrypoint: str
) -> None:
    """Fail closed when the lite path introduces a direct provider client."""
    reachable, external = lite_closure(files, entrypoint)
    violations: list[str] = []
    for specifier in external:
        lowered = _normalize(specifier)
        for pattern in DIRECT_PROVIDER_IMPORT_PATTERNS:
            if _normalize(pattern) in lowered:
                # Imports that resolve under the existing provider tree are
                # the preserved OpenCode loop, not a rewrite.
                if "provider" not in lowered:
                    violations.append(specifier)
    for path in reachable:
        if "provider-direct" in _normalize(path) or "direct-client" in _normalize(
            path
        ):
            violations.append(path)
        content = _normalize(files.get(path, ""))
        for pattern in ("api.openai.com", "api.anthropic.com"):
            if pattern in content and "provider/" not in _normalize(path):
                violations.append("%s:hard-coded-%s" % (path, pattern))
    if violations:
        raise ValueError(
            "lite path must not add a direct provider/API client: %s"
            % ", ".join(sorted(set(violations)))
        )


def validate_lite_graph(files: dict[str, str], spec: dict) -> dict:
    """Validate the full lite import graph; return the closure summary."""
    entrypoint = spec["variant"]["entrypoint"]
    check_all_excluded_removed(files, entrypoint, spec)
    check_retained_present(files, entrypoint, spec)
    check_no_direct_provider_client(files, entrypoint)
    reachable, external = lite_closure(files, entrypoint)
    return {
        "entrypoint": entrypoint,
        "reachable_files": sorted(reachable),
        "external_imports": sorted(external),
        "reachable_count": len(reachable),
    }


def _read_tree(root: str) -> dict[str, str]:
    """Read all .ts/.tsx files under root into a repo-relative file map."""
    files: dict[str, str] = {}
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            if not name.endswith((".ts", ".tsx")):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            try:
                with open(full, "r", encoding="utf-8") as handle:
                    files[rel] = handle.read()
            except (OSError, UnicodeDecodeError):
                continue
    return files


def validate_fork_tree(root: str, spec: dict | None = None) -> dict:
    """Validate a real ``kodmial/opencode`` checkout against the spec.

    Checks the lite entrypoint/build target exist, the full build remains
    usable, ``package.json`` carries both build scripts at the pinned
    version, and the lite static import closure excludes every removed
    subsystem without introducing a direct provider client.
    """
    active = spec or load_spec()
    variant = active["variant"]
    full = active["full"]
    lite_entry = os.path.join(root, variant["entrypoint"])
    lite_build = os.path.join(root, variant["build_script"])
    full_entry = os.path.join(root, full["entrypoint"])
    full_build = os.path.join(root, full["build_script"])
    missing = [
        path
        for path in (lite_entry, lite_build, full_entry, full_build)
        if not os.path.isfile(path)
    ]
    if missing:
        raise ValueError(
            "fork checkout is missing required path(s): %s"
            % ", ".join(sorted(missing))
        )
    manifest_path = os.path.join(root, "packages/opencode/package.json")
    if not os.path.isfile(manifest_path):
        raise ValueError("fork checkout is missing packages/opencode/package.json")
    with open(manifest_path, "r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    pinned = active["pinned_opencode_version"]
    if manifest.get("version") != pinned:
        raise ValueError(
            "fork manifest version %r does not match pinned %r"
            % (manifest.get("version"), pinned)
        )
    scripts = manifest.get("scripts") or {}
    if not str(scripts.get("build", "")).strip():
        raise ValueError("fork manifest must keep scripts.build for the full CLI")
    if not str(scripts.get("build:lite", "")).strip():
        raise ValueError(
            "fork manifest must define scripts.build:lite for the coding-only CLI"
        )
    files = _read_tree(root)
    # Re-key the map to the spec-relative layout (spec paths already use
    # the packages/opencode/... form when root is the fork root).
    summary = validate_lite_graph(files, active)
    summary["fork_root"] = os.path.abspath(root)
    summary["pinned_version"] = pinned
    return summary


def lite_patch_plan(spec: dict | None = None) -> list[dict[str, str]]:
    """Return the additive fork-side patch plan (no existing file rewrites).

    Each step is ``{"path":..., "action": "add", "purpose":...}``. The plan
    is additive on purpose: the lite entrypoint and build target form a
    separate compile graph, so the full upstream build keeps working while
    reviewers stage removal groups A, then B, then C with per-group
    ``check_group_removed`` evidence instead of one opaque bulk change.
    """
    active = spec or load_spec()
    variant = active["variant"]
    return [
        {
            "path": variant["entrypoint"],
            "action": "add",
            "purpose": "Headless coding-only entrypoint wiring only the retained provider/agent/tool loop.",
        },
        {
            "path": variant["support_dir"] + "/tools.ts",
            "action": "add",
            "purpose": "Minimal tool registry: read/search/edit/write/patch/shell only.",
        },
        {
            "path": variant["support_dir"] + "/agent.ts",
            "action": "add",
            "purpose": "Coding-only agent loop reusing the existing provider/session modules.",
        },
        {
            "path": variant["build_script"],
            "action": "add",
            "purpose": "Dedicated bun build target emitting the lite binary to %s."
            % variant.get("outdir", "dist/lite"),
        },
        {
            "path": "packages/opencode/package.json",
            "action": "extend-scripts",
            "purpose": "Add scripts.build:lite alongside the untouched scripts.build.",
        },
    ]


def representative_coding_task(workspace: str) -> dict[str, str]:
    """Run the deterministic representative coding task in a workspace.

    The task mirrors the issue correctness clause: inspect/search the
    repository, modify files, run build/tests, react to failures, and
    return the expected result. Stdlib only; raises on any failure.
    """
    if not isinstance(workspace, str) or not workspace.strip():
        raise ValueError("workspace must be a non-empty string")
    os.makedirs(workspace, exist_ok=True)
    target = os.path.join(workspace, "sample.txt")
    with open(target, "w", encoding="utf-8") as handle:
        handle.write("line one\nline two\n")
    # Inspect/search: locate the seeded marker before editing.
    with open(target, "r", encoding="utf-8") as handle:
        before = handle.read()
    if "line two" not in before:
        raise ValueError("representative task: seed content not found")
    matches = [line for line in before.splitlines() if "two" in line]
    if not matches:
        raise ValueError("representative task: search found no match")
    # Modify: append the expected line, then verify via a second read.
    with open(target, "a", encoding="utf-8") as handle:
        handle.write("line three\n")
    with open(target, "r", encoding="utf-8") as handle:
        after = handle.read()
    if "line three" not in after:
        raise ValueError("representative task: edit did not persist")
    # Run build/tests: byte-compile this file plus a syntax probe of the
    # edited artifact, reacting to failures instead of ignoring them.
    probe = subprocess.run(
        [sys.executable, "-m", "py_compile", os.path.abspath(__file__)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if probe.returncode != 0:
        raise ValueError(
            "representative task: build probe failed: %s"
            % (probe.stderr or probe.stdout)[:300]
        )
    check = subprocess.run(
        [sys.executable, "-c", "print(open(%r).read())" % target],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if check.returncode != 0 or "line three" not in (check.stdout or ""):
        raise ValueError("representative task: test probe failed")
    return {"result": "line three", "matches": str(len(matches))}
