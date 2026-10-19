#!/usr/bin/env python3
"""Read-only fresh-runner qualification of private Child durable model state.

No private names/IDs are included in tracked source, actions outputs or logs.
Only owner-owned private repositories with an explicitly installed rollout
marker are candidates. Never writes state, edits code or issues LLM requests.
"""
from __future__ import annotations
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

def fail(message):
    raise RuntimeError(message)

def api(path):
    try:
        proc = subprocess.run(["gh", "api", path], capture_output=True, text=True,
                              timeout=25, check=False)
    except (OSError, subprocess.TimeoutExpired):
        fail("Authenticated GitHub discovery temporarily unavailable")
    if proc.returncode:
        fail("Authenticated GitHub discovery temporarily unavailable")
    return json.loads(proc.stdout)

def candidate_repositories():
    # One bounded listing, not the expensive role-resolution scan of all repos.
    owner = os.environ["GITHUB_REPOSITORY"].split("/")[0].lower()
    repos = api("/user/repos?affiliation=owner&per_page=100")
    if not isinstance(repos, list) or len(repos) >= 100:
        fail("Owner repository inventory is incomplete")
    names = []
    for entry in repos:
        if not isinstance(entry, dict) or not entry.get("private"):
            continue
        name = entry.get("full_name", "")
        if (isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name)
                and name.split("/")[0].lower() == owner):
            # GitHub runner masks any private identity before subsequent usage.
            print("::add-mask::" + name, flush=True)
            names.append(name)
    return sorted(set(names))

def has_rollout_marker(repo):
    proc = subprocess.run(
        ["gh", "api", "repos/" + repo + "/contents/.github/workflows/continuum-model-rollout.yml"],
        capture_output=True, timeout=20, check=False)
    return proc.returncode == 0

def canonical_module():
    path = Path(os.environ["CONTINUUM_ENGINE_ROOT"]) / "src/continuum/model_routing.py"
    spec = importlib.util.spec_from_file_location("verified_continuum_routing", path)
    if not spec or not spec.loader:
        fail("Continuum canonical model selector missing")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module

def main():
    if not os.environ.get("GH_TOKEN"):
        fail("Private state read token unavailable")
    pool = os.environ.get("CONTINUUM_APPROVED_MODELS", "")
    if not pool:
        fail("Trusted model pool missing")
    expected_run = os.environ.get("EXPECTED_REAL_RUN", "")
    if not re.fullmatch(r"[0-9]{1,20}/[0-9]{1,8}", expected_run):
        fail("Expected exact earlier successful run receipt")
    routing = canonical_module()
    config = routing.Configuration.parse(pool, task_class="coder")
    version, digest = routing.binary_provenance()
    targets = 0
    for repo in candidate_repositories():
        if not has_rollout_marker(repo):
            continue
        targets += 1
        # Trust cannot be inferred from success alone: verify a trusted pool,
        # runtime binary digest and exactly the earlier real execution receipt.
        key = routing.route_key(repo, "consumer-model-rollout", "anonymous", config, digest)
        state, _, _ = routing.GitRefStore(repo, os.environ["GH_TOKEN"]).read(key)
        real = state.get("last_successful_real_execution") or {}
        if state.get("fingerprint") != config.fingerprint(digest, "anonymous"):
            fail("Private state fingerprint did not match trusted configuration")
        if state.get("runtime", {}).get("version") != version:
            fail("Verified OpenCode binary version does not match state")
        if real.get("run") != expected_run or real.get("model") not in config.models:
            fail("Private durable state is missing the exact earlier real LLM success")
        if not isinstance(real.get("at"), int) or abs(int(time.time()) - real["at"]) > 86400:
            fail("Private real-success receipt timestamp invalid")
        print("PASS: one isolated private Child durable real-model state verified.")
    if targets != 2:
        fail("Expected exactly two owner-approved private rollout targets")
    print("PASS: two independent private Child states verified on a fresh public runner.")

if __name__ == "__main__":
    try:
        main()
    except (KeyError, ValueError, RuntimeError, OSError, subprocess.SubprocessError):
        print("::error::Private model state validation failed; no LLM requests or state writes made.", file=sys.stderr)
        raise SystemExit(2)
