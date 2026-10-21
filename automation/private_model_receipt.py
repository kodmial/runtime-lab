#!/usr/bin/env python3
"""Authenticated encrypted handoff for private Child model receipts.

The first Parent job seals the discovered private targets with a key derived
from the trusted Parent token and Actions run ID. The independent fresh-runner
job unseals them and verifies only the durable Continuum state records written
by the first job. No private repository name or ID is emitted in public Parent
logs or job outputs, and the verifier never issues another model request.
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

try:
    from private_model_api import client_from_env, retry_delay
except ImportError:  # pragma: no cover - only used if helpers are split
    client_from_env = None
    retry_delay = None

RECEIPT_VERSION = 1
MAX_EXECUTION_AGE = 6 * 60 * 60
MAX_TRANSIENT_WAIT = 120
TRANSIENT_STATUS = re.compile(r"failed \((\d{3})\)")


def fail(message):
    raise RuntimeError(message)


def _run_identity(environ=None):
    environ = os.environ if environ is None else environ
    run = environ.get("GITHUB_RUN_ID", "")
    attempt = environ.get("GITHUB_RUN_ATTEMPT", "")
    if not re.fullmatch(r"\d{1,20}", run) or not re.fullmatch(r"\d{1,8}", attempt):
        fail("Trusted Parent run identity unavailable")
    return run, attempt


def keys(environ=None):
    environ = os.environ if environ is None else environ
    secret = environ.get("GH_TOKEN", "")
    run, _ = _run_identity(environ)
    if len(secret) < 16:
        fail("Trusted Parent token unavailable")
    # Bind the key to the run, not the attempt, so a failed-only rerun can
    # still decrypt the receipt emitted by the earlier successful job.
    seed = hmac.new(
        secret.encode(),
        ("continuum/private-receipt-v1/" + run).encode(),
        hashlib.sha256,
    ).digest()
    return (
        hmac.new(seed, b"encryption", hashlib.sha256).digest(),
        hmac.new(seed, b"authentication", hashlib.sha256).digest(),
    )


def crypt(message, key, iv, decrypt=False):
    command = [
        "openssl",
        "enc",
        "-aes-256-cbc",
        "-K",
        key.hex(),
        "-iv",
        iv.hex(),
        "-nosalt",
    ]
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
    return base64.urlsafe_b64encode(
        body + hmac.new(mac, body, hashlib.sha256).digest()
    ).decode()


def unseal(token):
    enc, mac = keys()
    try:
        data = base64.urlsafe_b64decode(token.encode())
    except (ValueError, base64.binascii.Error):
        fail("Malformed sealed receipt")
    if len(data) < 65 or data[0] != RECEIPT_VERSION:
        fail("Malformed sealed receipt")
    body, tag = data[:-32], data[-32:]
    if not hmac.compare_digest(tag, hmac.new(mac, body, hashlib.sha256).digest()):
        fail("Sealed receipt authentication failed")
    return crypt(body[17:], enc, body[1:17], True)


def _approved_models(pool):
    choices = [item.strip() for item in pool.split(",")]
    if not choices or any(not item for item in choices):
        fail("Trusted approved model pool is invalid")
    return set(choices)


def validate(value, pool, environ=None):
    environ = os.environ if environ is None else environ
    if not isinstance(value, dict) or set(value) != {
        "owner",
        "run",
        "attempt",
        "pool_hash",
        "targets",
    }:
        fail("Invalid receipt structure")
    run, attempt = _run_identity(environ)
    owner = environ.get("GITHUB_REPOSITORY", "")
    if not isinstance(value["owner"], str) or not isinstance(value["run"], str):
        fail("Invalid receipt identity")
    if value["owner"] != owner or value["run"] != run:
        fail("Receipt scope does not match trusted Parent workflow")
    if not isinstance(value["attempt"], str) or not re.fullmatch(
        r"\d{1,8}", value["attempt"]
    ):
        fail("Invalid receipt run attempt")
    if not 1 <= int(value["attempt"]) <= int(attempt):
        fail("Receipt attempt is newer than the trusted run")
    if value["pool_hash"] != hashlib.sha256(pool.encode()).hexdigest():
        fail("Approved model pool changed")
    targets = value["targets"]
    if not isinstance(targets, list) or len(targets) != 2:
        fail("Unexpected target count")
    approved = _approved_models(pool)
    seen = set()
    for target in targets:
        if not isinstance(target, dict) or set(target) != {"repository", "model"}:
            fail("Invalid private target receipt")
        repo, model = target["repository"], target["model"]
        if not isinstance(repo, str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo
        ):
            fail("Malformed sealed repository identity")
        normalized = repo.lower()
        owner_match = (
            repo.split("/")[0].lower() == owner.split("/")[0].lower()
        )
        if not owner_match or normalized in seen:
            fail("Unauthorized or duplicate private repository")
        if not isinstance(model, str) or model not in approved:
            fail("Model differs from approved pool")
        seen.add(normalized)
    return targets


def routing_module():
    root = Path(os.environ["CONTINUUM_ENGINE_ROOT"])
    path = root / "src" / "continuum" / "model_routing.py"
    spec = importlib.util.spec_from_file_location("continuum_model_routing", path)
    if not spec or not spec.loader:
        fail("Canonical Continuum selector unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _transient_status(error):
    text = str(error)
    match = TRANSIENT_STATUS.search(text)
    if match:
        status = int(match.group(1))
        return status if status == 429 or 500 <= status <= 599 else None
    if "backend unavailable" in text:
        return 0
    return None


def _wait_before_retry(status, attempt):
    if status == 429 and client_from_env is not None:
        client_from_env().wait_for_reset(max_wait=MAX_TRANSIENT_WAIT)
        return
    if retry_delay is not None:
        delay = retry_delay({}, attempt)
    else:  # pragma: no cover
        delay = [5, 15][min(attempt, 1)]
    time.sleep(min(delay, MAX_TRANSIENT_WAIT))


def read_state(store, key, routing, *, attempts=3):
    for attempt in range(attempts):
        try:
            return store.read(key)
        except routing.RouteError as error:
            status = _transient_status(error)
            if status is None:
                fail("Private Child Git state unavailable or access refused")
            if attempt + 1 >= attempts:
                fail("Private Child state backend unavailable after bounded retries")
            _wait_before_retry(status, attempt)
    raise AssertionError("unreachable")


def verify(value, pool, *, routing=None, environ=None):
    environ = os.environ if environ is None else environ
    targets = validate(value, pool, environ)
    routing = routing or routing_module()
    config = routing.Configuration.parse(pool, task_class="coder")
    version, digest = routing.binary_provenance()
    expected_run = value["run"] + "/" + value["attempt"]
    token = environ.get("GH_TOKEN", "")
    for target in targets:
        repository, expected = target["repository"], target["model"]
        key = routing.route_key(
            repository, "consumer-model-rollout", "anonymous", config, digest
        )
        state, _, _ = read_state(
            routing.GitRefStore(repository, token), key, routing
        )
        success = state.get("last_successful_real_execution") or {}
        if state.get("fingerprint") != config.fingerprint(digest, "anonymous"):
            fail("Private Child state fingerprint mismatch")
        runtime = state.get("runtime") or {}
        if runtime.get("version") != version or runtime.get("sha256") != digest:
            fail("Private Child runtime provenance mismatch")
        if success.get("run") != expected_run or success.get("model") != expected:
            fail("Independent-runner real-success state mismatch")
        if not isinstance(success.get("at"), int) or abs(
            int(time.time()) - success["at"]
        ) > MAX_EXECUTION_AGE:
            fail("Real-success state expired")
        print(
            "Verified private Child: same approved model and real execution, "
            "private durable Git state."
        )
    print(
        "PASS: %d isolated private Child receipts verified on independent "
        "public runner." % len(targets)
    )


def main():
    pool = os.environ.get("CONTINUUM_APPROVED_MODELS", "")
    if not pool:
        fail("Trusted approved model pool missing")
    operation = sys.argv[1] if len(sys.argv) > 1 else ""
    if operation == "seal" and len(sys.argv) == 3:
        targets = [
            json.loads(line)
            for line in Path(sys.argv[2]).read_text().splitlines()
            if line
        ]
        value = {
            "owner": os.environ.get("GITHUB_REPOSITORY", ""),
            "run": os.environ.get("GITHUB_RUN_ID", ""),
            "attempt": os.environ.get("GITHUB_RUN_ATTEMPT", ""),
            "pool_hash": hashlib.sha256(pool.encode()).hexdigest(),
            "targets": targets,
        }
        validate(value, pool)
        print(
            seal(
                json.dumps(
                    value, sort_keys=True, separators=(",", ":")
                ).encode()
            )
        )
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
        print(
            "::error::Private model receipt verification failed",
            file=sys.stderr,
        )
        raise SystemExit(2)
