#!/usr/bin/env python3
"""Authenticated encrypted handoff for private Child model receipts between Parent jobs.

No private repository name or ID is emitted in public Parent logs or job outputs.
The verifier reads canonical Continuum state without issuing another LLM request.
"""
from __future__ import annotations
import base64
import hashlib
import hmac
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

def keys():
    secret = os.environ.get("GH_TOKEN", "")
    run = os.environ.get("GITHUB_RUN_ID", "")
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "")
    if len(secret) < 16 or not re.fullmatch(r"\d{1,20}", run) or not re.fullmatch(r"\d{1,8}", attempt):
        fail("Trusted Parent identity/token unavailable")
    seed = hmac.new(secret.encode(), f"continuum/private-receipt-v1/{run}/{attempt}".encode(), hashlib.sha256).digest()
    return (hmac.new(seed, b"encryption", hashlib.sha256).digest(),
            hmac.new(seed, b"authentication", hashlib.sha256).digest())

def crypt(message, key, iv, decrypt=False):
    command = ["openssl", "enc", "-aes-256-cbc", "-K", key.hex(), "-iv", iv.hex(), "-nosalt"]
    if decrypt:
        command.insert(2, "-d")
    result = subprocess.run(command, input=message, capture_output=True, timeout=15)
    if result.returncode:
        fail("Encrypted handoff processing failed")
    return result.stdout

def seal(raw):
    enc, mac = keys()
    iv = os.urandom(16)
    body = b"\x01" + iv + crypt(raw, enc, iv)
    return base64.urlsafe_b64encode(body + hmac.new(mac, body, hashlib.sha256).digest()).decode()

def unseal(token):
    enc, mac = keys()
    try:
        data = base64.urlsafe_b64decode(token.encode())
    except (ValueError, base64.binascii.Error):
        fail("Malformed sealed receipt")
    if len(data) < 65 or data[0] != 1:
        fail("Malformed sealed receipt")
    body, tag = data[:-32], data[-32:]
    if not hmac.compare_digest(tag, hmac.new(mac, body, hashlib.sha256).digest()):
        fail("Sealed receipt authentication failed")
    return crypt(body[17:], enc, body[1:17], True)

def validate(value, pool):
    if not isinstance(value, dict) or set(value) != {"owner", "run", "attempt", "pool_hash", "targets"}:
        fail("Invalid receipt structure")
    owner = os.environ.get("GITHUB_REPOSITORY", "")
    if value["owner"] != owner or value["run"] != os.environ.get("GITHUB_RUN_ID") or value["attempt"] != os.environ.get("GITHUB_RUN_ATTEMPT"):
        fail("Receipt scope does not match trusted Parent workflow")
    if value["pool_hash"] != hashlib.sha256(pool.encode()).hexdigest():
        fail("Approved model pool changed")
    targets = value["targets"]
    if not isinstance(targets, list) or not 2 <= len(targets) <= 20:
        fail("Unexpected target count")
    seen = set()
    for target in targets:
        if not isinstance(target, dict) or set(target) != {"repository", "model"}:
            fail("Invalid private target receipt")
        repo, model = target["repository"], target["model"]
        if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            fail("Malformed sealed repository identity")
        if repo.split("/")[0].lower() != owner.split("/")[0].lower() or repo in seen:
            fail("Unauthorized or duplicate private repository")
        if not isinstance(model, str) or model not in pool.split(","):
            fail("Model differs from approved pool")
        seen.add(repo)
    return targets

def routing_module():
    path = Path(os.environ["CONTINUUM_ENGINE_ROOT"]) / "src" / "continuum" / "model_routing.py"
    spec = importlib.util.spec_from_file_location("continuum_model_routing", path)
    if not spec or not spec.loader:
        fail("Canonical Continuum selector unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module

def verify(value, pool):
    targets = validate(value, pool)
    routing = routing_module()
    config = routing.Configuration.parse(pool, task_class="coder")
    version, digest = routing.binary_provenance()
    expected_run = value["run"] + "/" + value["attempt"]
    for target in targets:
        repository, expected = target["repository"], target["model"]
        key = routing.route_key(repository, "consumer-model-rollout", "anonymous", config, digest)
        store = routing.GitRefStore(repository, os.environ["GH_TOKEN"])
        state = None
        for attempt in range(3):
            try:
                state, _, _ = store.read(key)
                break
            except routing.RouteError as error:
                if "backend unavailable" not in str(error) and "failed (5" not in str(error):
                    fail("Private Child Git state unavailable or access refused")
                if attempt == 2:
                    fail("Private Child state backend unavailable after bounded retries")
                time.sleep([5, 15][attempt])
        success = state.get("last_successful_real_execution") or {}
        if state.get("fingerprint") != config.fingerprint(digest, "anonymous"):
            fail("Private Child state fingerprint mismatch")
        if state.get("runtime", {}).get("version") != version:
            fail("Private Child runtime provenance mismatch")
        if success.get("run") != expected_run or success.get("model") != expected:
            fail("Independent-runner real-success state mismatch")
        if not isinstance(success.get("at"), int) or abs(int(time.time()) - success["at"]) > 3600:
            fail("Real-success state expired")
        print("Verified private Child: same approved model and real execution, private durable Git state.")
    print(f"PASS: {len(targets)} isolated private Child receipts verified on independent public runner.")

def main():
    pool = os.environ.get("CONTINUUM_APPROVED_MODELS", "")
    if not pool:
        fail("Trusted approved model pool missing")
    operation = sys.argv[1] if len(sys.argv) > 1 else ""
    if operation == "seal" and len(sys.argv) == 3:
        targets = [json.loads(s) for s in Path(sys.argv[2]).read_text().splitlines() if s]
        value = {"owner": os.environ.get("GITHUB_REPOSITORY", ""),
                 "run": os.environ.get("GITHUB_RUN_ID", ""),
                 "attempt": os.environ.get("GITHUB_RUN_ATTEMPT", ""),
                 "pool_hash": hashlib.sha256(pool.encode()).hexdigest(),
                 "targets": targets}
        validate(value, pool)
        print(seal(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()))
    elif operation == "verify" and len(sys.argv) == 2:
        ciphertext = os.environ.get("CONTINUUM_SEALED_RECEIPT", "")
        if not ciphertext:
            fail("Encrypted private receipt handoff missing")
        verify(json.loads(unseal(ciphertext)), pool)
    else:
        fail("Invalid private receipt operation")

if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, KeyError, OSError, subprocess.SubprocessError):
        # Deliberately generic: API exceptions must never expose private repos.
        print("::error::Private model receipt verification failed", file=sys.stderr)
        raise SystemExit(2)
