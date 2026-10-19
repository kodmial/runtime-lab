#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import os
import pathlib
import shutil
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import private_model_receipt as receipt  # noqa: E402


class Config:
    models = ("approved/model",)

    @classmethod
    def parse(cls, pool, **kwargs):
        return cls()

    def fingerprint(self, digest, scope):
        return "fingerprint:" + digest + ":" + scope


class RouteError(RuntimeError):
    pass


class FakeStore:
    states = []
    errors = []
    reads = 0

    def __init__(self, repository, token):
        self.repository = repository
        self.token = token

    def read(self, key):
        FakeStore.reads += 1
        if FakeStore.errors:
            raise FakeStore.errors.pop(0)
        return FakeStore.states.pop(0), None, None


class FakeRouting:
    RouteError = RouteError
    Configuration = Config
    GitRefStore = FakeStore

    @staticmethod
    def binary_provenance():
        return "1.19.3", "binary-digest"

    @staticmethod
    def route_key(repository, profile, scope, config, digest):
        return "route-key"


def environment(attempt="1"):
    return {
        "GH_TOKEN": "ghp_" + "x" * 40,
        "GITHUB_RUN_ID": "39318890383",
        "GITHUB_RUN_ATTEMPT": attempt,
        "GITHUB_REPOSITORY": "kodmial/runtime-lab",
    }


def value_for(attempt="1"):
    return {
        "owner": "kodmial/runtime-lab",
        "run": "39318890383",
        "attempt": attempt,
        "pool_hash": hashlib.sha256(b"approved/model").hexdigest(),
        "targets": [
            {"repository": "kodmial/private-one", "model": "approved/model"},
            {"repository": "kodmial/private-two", "model": "approved/model"},
        ],
    }


def state_for(attempt="1"):
    return {
        "fingerprint": "fingerprint:binary-digest:anonymous",
        "runtime": {"version": "1.19.3", "sha256": "binary-digest"},
        "last_successful_real_execution": {
            "run": "39318890383/" + attempt,
            "model": "approved/model",
            "at": int(time.time()),
        },
    }


class ReceiptValidationTests(unittest.TestCase):
    def test_valid_receipt_is_accepted(self):
        targets = receipt.validate(value_for(), "approved/model", environment())
        self.assertEqual(len(targets), 2)

    def test_no_private_identity_is_printed_on_success(self):
        FakeStore.states = [state_for(), state_for()]
        FakeStore.errors = []
        FakeStore.reads = 0
        with mock.patch.dict(os.environ, environment(), clear=False):
            with mock.patch("builtins.print") as printed:
                receipt.verify(value_for(), "approved/model", routing=FakeRouting)
        output = "\n".join(str(call.args[0]) for call in printed.call_args_list)
        self.assertIn("PASS: 2 isolated private Child receipts verified", output)
        self.assertNotIn("private-one", output)
        self.assertNotIn("private-two", output)

    def test_wrong_run_is_rejected(self):
        value = value_for()
        value["run"] = "1"
        with self.assertRaises(RuntimeError):
            receipt.validate(value, "approved/model", environment())

    def test_newer_attempt_is_rejected(self):
        with self.assertRaises(RuntimeError):
            receipt.validate(value_for("2"), "approved/model", environment("1"))

    def test_earlier_attempt_is_allowed_for_failed_only_rerun(self):
        receipt.validate(value_for("1"), "approved/model", environment("2"))

    def test_non_approved_model_is_rejected(self):
        value = value_for()
        value["targets"][0]["model"] = "other/model"
        with self.assertRaises(RuntimeError):
            receipt.validate(value, "approved/model", environment())

    def test_duplicate_repository_is_rejected_case_insensitively(self):
        value = value_for()
        value["targets"][1]["repository"] = "Kodmial/PRIVATE-ONE"
        with self.assertRaises(RuntimeError):
            receipt.validate(value, "approved/model", environment())

    def test_foreign_owner_is_rejected(self):
        value = value_for()
        value["targets"][0]["repository"] = "someone/private-one"
        with self.assertRaises(RuntimeError):
            receipt.validate(value, "approved/model", environment())

    def test_plaintext_identities_only_exist_inside_sealed_payload(self):
        if not shutil.which("openssl"):
            self.skipTest("openssl is required for the sealed handoff")
        with mock.patch.dict(os.environ, environment(), clear=False):
            sealed = receipt.seal(b"private marker")
        self.assertNotIn("private", sealed)
        with mock.patch.dict(os.environ, environment("2"), clear=False):
            self.assertEqual(receipt.unseal(sealed), b"private marker")


class ReceiptVerificationTests(unittest.TestCase):
    def setUp(self):
        FakeStore.states = []
        FakeStore.errors = []
        FakeStore.reads = 0

    def verify(self, state=None):
        FakeStore.states = [state or state_for(), state or state_for()]
        with mock.patch.dict(os.environ, environment(), clear=False):
            receipt.verify(value_for(), "approved/model", routing=FakeRouting)

    def test_runtime_digest_mismatch_fails(self):
        state = state_for()
        state["runtime"]["sha256"] = "different"
        with self.assertRaises(RuntimeError):
            self.verify(state)

    def test_fingerprint_mismatch_fails(self):
        state = state_for()
        state["fingerprint"] = "different"
        with self.assertRaises(RuntimeError):
            self.verify(state)

    def test_success_run_mismatch_fails(self):
        state = state_for()
        state["last_successful_real_execution"]["run"] = "39318890383/9"
        with self.assertRaises(RuntimeError):
            self.verify(state)

    def test_expired_success_fails(self):
        state = state_for()
        state["last_successful_real_execution"]["at"] = int(time.time()) - 30_000
        with self.assertRaises(RuntimeError):
            self.verify(state)

    def test_transient_500_is_retried(self):
        FakeStore.states = [state_for(), state_for()]
        FakeStore.errors = [RouteError("GitHub model state GET failed (500)")]
        # One error is consumed by the first repository, then both succeed.
        with mock.patch.dict(os.environ, environment(), clear=False):
            with mock.patch.object(receipt.time, "sleep", return_value=None) as sleep:
                receipt.verify(value_for(), "approved/model", routing=FakeRouting)
        self.assertTrue(sleep.called)

    def test_permission_403_is_not_retried(self):
        FakeStore.errors = [RouteError("GitHub model state GET failed (403)")]
        with mock.patch.dict(os.environ, environment(), clear=False):
            with self.assertRaises(RuntimeError):
                receipt.verify(value_for(), "approved/model", routing=FakeRouting)
        self.assertEqual(FakeStore.reads, 1)

    def test_429_waits_for_documented_reset(self):
        FakeStore.states = [state_for(), state_for()]
        FakeStore.errors = [RouteError("GitHub model state GET failed (429)")]
        client = mock.Mock()
        with mock.patch.dict(os.environ, environment(), clear=False):
            with mock.patch.object(receipt, "client_from_env", return_value=client):
                receipt.verify(value_for(), "approved/model", routing=FakeRouting)
        client.wait_for_reset.assert_called_once_with(max_wait=120)


if __name__ == "__main__":
    unittest.main()
