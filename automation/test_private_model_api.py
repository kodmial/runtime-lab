#!/usr/bin/env python3
from __future__ import annotations

import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from private_model_api import (  # noqa: E402
    GitHubApiError,
    GitHubClient,
    child_repository_map,
    ensure_variable,
    has_marker,
    metadata,
    repository_variables,
    retry_delay,
)


class FakeClock:
    def __init__(self, now=1_000):
        self.now = now

    def __call__(self):
        return self.now


class Recorder:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, data, timeout):
        self.calls.append((method, url, data))
        if not self.responses:
            raise AssertionError("unexpected request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def response(status, body=b"{}", headers=None):
    return status, headers or {}, body


class RetryDelayTests(unittest.TestCase):
    def test_retry_after_seconds_is_honored(self):
        self.assertEqual(retry_delay({"Retry-After": "7"}, 0, now=100), 7)

    def test_rate_limit_reset_is_honored(self):
        delay = retry_delay({"X-RateLimit-Reset": "125"}, 0, now=100)
        self.assertEqual(delay, 26)

    def test_reset_beyond_bound_fails_instead_of_sleeping(self):
        with self.assertRaises(GitHubApiError) as raised:
            retry_delay({"Retry-After": "9999"}, 0, now=100)
        self.assertEqual(raised.exception.status, 429)


class RequestTests(unittest.TestCase):
    def test_permission_403_is_not_retried(self):
        recorder = Recorder([response(403, b'{"message":"forbidden"}')])
        client = GitHubClient(
            "token",
            request_fn=recorder,
            sleep_fn=lambda _: None,
            clock_fn=FakeClock(),
        )
        with self.assertRaises(GitHubApiError) as raised:
            client.request("GET", "repos/example/private")
        self.assertEqual(raised.exception.status, 403)
        self.assertEqual(len(recorder.calls), 1)
        self.assertNotIn("example", str(raised.exception))

    def test_transient_500_is_retried_once(self):
        recorder = Recorder(
            [response(500, b""), response(200, b'{"private":true}')]
        )
        sleeps = []
        client = GitHubClient(
            "token",
            request_fn=recorder,
            sleep_fn=sleeps.append,
            clock_fn=FakeClock(),
        )
        self.assertEqual(
            client.request("GET", "repos/example/private"),
            {"private": True},
        )
        self.assertEqual(len(recorder.calls), 2)
        self.assertEqual(sleeps, [5])

    def test_429_obeys_retry_after(self):
        recorder = Recorder(
            [response(429, b"", {"Retry-After": "9"}), response(200, b"{}")]
        )
        sleeps = []
        client = GitHubClient(
            "token",
            request_fn=recorder,
            sleep_fn=sleeps.append,
            clock_fn=FakeClock(),
        )
        self.assertEqual(client.request("GET", "rate_limit"), {})
        self.assertEqual(sleeps, [9])

    def test_404_is_permanent_unless_missing_is_allowed(self):
        recorder = Recorder([response(404, b"")])
        client = GitHubClient(
            "token", request_fn=recorder, sleep_fn=lambda _: None
        )
        with self.assertRaises(GitHubApiError):
            client.request("GET", "repos/example/private")

    def test_rate_budget_waits_for_documented_reset(self):
        reset = 1_000 + 100
        recorder = Recorder(
            [
                response(
                    200,
                    (b'{"resources":{"core":{"remaining":10,"limit":5000,'
                     b'"reset":%d}}}' % reset),
                )
            ]
        )
        clock = FakeClock()
        sleeps = []
        client = GitHubClient(
            "token",
            request_fn=recorder,
            sleep_fn=sleeps.append,
            clock_fn=clock,
        )
        self.assertEqual(client.rate_budget(threshold=80, max_wait=200), (10, 5000))
        self.assertEqual(sleeps, [110])

    def test_rate_budget_fails_when_reset_exceeds_bound(self):
        recorder = Recorder(
            [
                response(
                    200,
                    b'{"resources":{"core":{"remaining":1,"limit":5000,"reset":99999}}}',
                )
            ]
        )
        client = GitHubClient(
            "token",
            request_fn=recorder,
            sleep_fn=lambda _: None,
            clock_fn=FakeClock(),
        )
        with self.assertRaises(GitHubApiError) as raised:
            client.rate_budget(threshold=80, max_wait=60)
        self.assertEqual(raised.exception.status, 429)


class VariableTests(unittest.TestCase):
    def test_metadata_reports_private_state(self):
        recorder = Recorder([response(200, b'{"private":true}')])
        client = GitHubClient("token", request_fn=recorder)
        self.assertTrue(metadata(client, "example/private"))

    def test_marker_missing_is_not_an_error(self):
        recorder = Recorder([response(404, b"")])
        client = GitHubClient("token", request_fn=recorder)
        self.assertFalse(has_marker(client, "example/private"))

    def test_ensure_variable_skips_write_when_policy_matches(self):
        recorder = Recorder([response(200, b'{"value":"a/b,c/d"}')])
        client = GitHubClient("token", request_fn=recorder)
        ensure_variable(client, "example/private", "a/b,c/d")
        self.assertEqual(len(recorder.calls), 1)
        self.assertEqual(recorder.calls[0][0], "GET")

    def test_ensure_variable_creates_and_reads_back(self):
        recorder = Recorder(
            [
                response(404, b""),
                response(201, b"{}"),
                response(200, b'{"value":"a/b"}'),
            ]
        )
        client = GitHubClient("token", request_fn=recorder)
        ensure_variable(client, "example/private", "a/b")
        self.assertEqual([call[0] for call in recorder.calls], ["GET", "POST", "GET"])

    def test_ensure_variable_readback_mismatch_fails(self):
        recorder = Recorder(
            [
                response(200, b'{"value":"old/model"}'),
                response(204, b""),
                response(200, b'{"value":"old/model"}'),
            ]
        )
        client = GitHubClient("token", request_fn=recorder)
        with self.assertRaises(GitHubApiError):
            ensure_variable(client, "example/private", "a/b")


class DiscoveryTests(unittest.TestCase):
    def variables(self, values):
        return response(
            200,
            json.dumps(
                {"total_count": len(values), "variables": values}
            ).encode(),
        )

    def test_repository_variables_are_paginated(self):
        first = [
            {"name": "VAR_%03d" % index, "value": "x"} for index in range(100)
        ]
        recorder = Recorder(
            [
                response(
                    200,
                    json.dumps(
                        {"total_count": 101, "variables": first}
                    ).encode(),
                ),
                response(
                    200,
                    json.dumps(
                        {
                            "total_count": 101,
                            "variables": [{"name": "LAST", "value": "y"}],
                        }
                    ).encode(),
                ),
            ]
        )
        client = GitHubClient("token", request_fn=recorder)
        values = repository_variables(client, "example/private")
        self.assertEqual(values["VAR_000"], "x")
        self.assertEqual(values["LAST"], "y")
        self.assertIn("page=2", recorder.calls[1][1])

    def test_repository_variables_requires_total_count(self):
        first = [
            {"name": "VAR_%03d" % index, "value": "x"} for index in range(100)
        ]
        recorder = Recorder(
            [response(200, json.dumps({"variables": first}).encode())]
        )
        client = GitHubClient("token", request_fn=recorder)
        with self.assertRaises(GitHubApiError):
            repository_variables(client, "example/private")

    def test_child_map_stops_after_all_allowed_children_are_mapped(self):
        parent = "example/parent"
        recorder = Recorder(
            [
                self.variables(
                    [
                        {"name": "CONTINUUM_ROLE", "value": "parent"},
                        {
                            "name": "CONTINUUM_CHILDREN",
                            "value": json.dumps(["child-a", "child-b"]),
                        },
                    ]
                ),
                response(
                    200,
                    json.dumps(
                        [
                            {
                                "full_name": "example/child-one",
                                "private": True,
                            },
                            {
                                "full_name": "example/child-two",
                                "private": True,
                            },
                            {
                                "full_name": "example/unrelated",
                                "private": True,
                            },
                        ]
                    ).encode(),
                ),
                self.variables(
                    [
                        {"name": "CONTINUUM_ROLE", "value": "child"},
                        {
                            "name": "CONTINUUM_CHILD_ID",
                            "value": "child-a",
                        },
                        {
                            "name": "CONTINUUM_PARENT",
                            "value": parent,
                        },
                    ]
                ),
                self.variables(
                    [
                        {"name": "CONTINUUM_ROLE", "value": "child"},
                        {
                            "name": "CONTINUUM_CHILD_ID",
                            "value": "child-b",
                        },
                        {
                            "name": "CONTINUUM_PARENT",
                            "value": parent,
                        },
                    ]
                ),
            ]
        )
        client = GitHubClient("token", request_fn=recorder)
        mapping = child_repository_map(client, parent)
        self.assertEqual(
            mapping,
            {
                "child-a": "example/child-one",
                "child-b": "example/child-two",
            },
        )
        self.assertEqual(len(recorder.calls), 4)
        self.assertEqual(recorder.responses, [])

    def test_child_map_uses_one_inventory_and_only_private_children(self):
        parent = "example/parent"
        children = ["child-a", "child-b"]
        recorder = Recorder(
            [
                self.variables(
                    [
                        {"name": "CONTINUUM_ROLE", "value": "parent"},
                        {
                            "name": "CONTINUUM_CHILDREN",
                            "value": json.dumps(children),
                        },
                    ]
                ),
                response(
                    200,
                    json.dumps(
                        [
                            {
                                "full_name": parent,
                                "private": False,
                            },
                            {
                                "full_name": "example/child-one",
                                "private": True,
                            },
                            {
                                "full_name": "example/public-child",
                                "private": False,
                            },
                            {
                                "full_name": "example/child-two",
                                "private": True,
                            },
                        ]
                    ).encode(),
                ),
                self.variables(
                    [
                        {"name": "CONTINUUM_ROLE", "value": "child"},
                        {
                            "name": "CONTINUUM_CHILD_ID",
                            "value": "child-a",
                        },
                        {
                            "name": "CONTINUUM_PARENT",
                            "value": parent,
                        },
                    ]
                ),
                self.variables(
                    [
                        {"name": "CONTINUUM_ROLE", "value": "child"},
                        {
                            "name": "CONTINUUM_CHILD_ID",
                            "value": "child-b",
                        },
                        {
                            "name": "CONTINUUM_PARENT",
                            "value": parent,
                        },
                    ]
                ),
            ]
        )
        client = GitHubClient("token", request_fn=recorder)
        mapping = child_repository_map(client, parent)
        self.assertEqual(
            mapping,
            {
                "child-a": "example/child-one",
                "child-b": "example/child-two",
            },
        )
        self.assertEqual(len(recorder.calls), 4)

    def test_incomplete_child_map_falls_back_without_guessing(self):
        recorder = Recorder(
            [
                self.variables(
                    [
                        {"name": "CONTINUUM_ROLE", "value": "parent"},
                        {
                            "name": "CONTINUUM_CHILDREN",
                            "value": json.dumps(["child-a", "child-b"]),
                        },
                    ]
                ),
                response(
                    200,
                    json.dumps(
                        [
                            {
                                "full_name": "example/child-one",
                                "private": True,
                            }
                        ]
                    ).encode(),
                ),
                self.variables(
                    [
                        {"name": "CONTINUUM_ROLE", "value": "child"},
                        {
                            "name": "CONTINUUM_CHILD_ID",
                            "value": "child-a",
                        },
                        {
                            "name": "CONTINUUM_PARENT",
                            "value": "example/parent",
                        },
                    ]
                ),
            ]
        )
        client = GitHubClient("token", request_fn=recorder)
        self.assertIsNone(child_repository_map(client, "example/parent"))

    def test_non_parent_role_is_rejected(self):
        recorder = Recorder(
            [
                self.variables(
                    [{"name": "CONTINUUM_ROLE", "value": "child"}]
                )
            ]
        )
        client = GitHubClient("token", request_fn=recorder)
        with self.assertRaises(GitHubApiError):
            child_repository_map(client, "example/parent")


if __name__ == "__main__":
    unittest.main()
