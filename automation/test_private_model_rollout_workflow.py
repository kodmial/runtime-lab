#!/usr/bin/env python3
from __future__ import annotations

import pathlib
import sys
import unittest

try:
    import yaml
except ImportError:  # pragma: no cover - CI installs PyYAML
    yaml = None

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github/workflows/continuum-private-model-rollout.yml"


def duplicate_keys(node, path=""):
    if yaml is None:
        return []
    findings = []
    if isinstance(node, yaml.MappingNode):
        seen = set()
        for key_node, value_node in node.value:
            key = getattr(key_node, "value", repr(key_node))
            if key in seen:
                findings.append(path + str(key))
            seen.add(key)
            findings.extend(duplicate_keys(value_node, path + str(key) + "."))
    elif isinstance(node, yaml.SequenceNode):
        for index, value_node in enumerate(node.value):
            findings.extend(duplicate_keys(value_node, path + str(index) + "."))
    return findings


@unittest.skipUnless(yaml is not None, "PyYAML is required for workflow validation")
class WorkflowValidationTests(unittest.TestCase):
    def load(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertEqual(duplicate_keys(yaml.compose(text)), [])
        return text, yaml.safe_load(text)

    def test_workflow_has_exactly_two_jobs(self):
        _, data = self.load()
        self.assertEqual(
            sorted(data["jobs"]), ["fresh-runner", "provision-and-prove"]
        )
        self.assertEqual(data["permissions"], {"contents": "read"})

    def test_workflow_dispatch_and_push_triggers_are_present(self):
        _, data = self.load()
        triggers = data.get("on", data.get(True))
        self.assertIn("workflow_dispatch", triggers)
        self.assertIn("main", triggers["push"]["branches"])
        self.assertIn(
            "automation/private_model_receipt.py", triggers["push"]["paths"]
        )
        self.assertIn("automation/private_model_api.py", triggers["push"]["paths"])

    def test_first_job_discovers_qualifies_and_seals(self):
        _, data = self.load()
        job = data["jobs"]["provision-and-prove"]
        self.assertEqual(job["outputs"]["sealed"], "${{ steps.run.outputs.sealed }}")
        scripts = "\n".join(
            step.get("run", "") for step in job["steps"] if isinstance(step, dict)
        )
        self.assertIn("delegation_repository.sh", scripts)
        self.assertIn("private_model_api.py rate-budget", scripts)
        self.assertIn("private_model_api.py child-map", scripts)
        self.assertIn("private_model_api.py ensure-variable", scripts)
        self.assertIn("private-child-map.json", scripts)
        self.assertIn('--plan "$RUNNER_TEMP/private-child-plan.json"', scripts)
        plan_at = scripts.index(
            'bash "$CONTINUUM_ENGINE_ROOT/.github/scripts/'
            'delegation_repository.sh" plan'
        )
        child_map_at = scripts.index("private_model_api.py child-map")
        # The resolver's plan is produced first so the variables-admission
        # proof is bounded by its authorized inventory instead of scanning
        # every private repository owned by the account.
        self.assertLess(plan_at, child_map_at)
        self.assertIn("model_routing.py\" select", scripts)
        self.assertIn("opencode run", scripts)
        self.assertIn("model_routing.py\" record", scripts)
        self.assertIn("private_model_receipt.py seal", scripts)
        self.assertIn('test "$count" -eq "$expected_targets"', scripts)
        self.assertIn("CONTINUUM_PRIVATE_ROLLOUT_TARGETS", scripts)
        self.assertIn("refusing to fall back to secret", scripts)
        self.assertIn("env -i", scripts)
        self.assertIn("CI=true", scripts)
        self.assertIn('OPENCODE_MODELS="$OPENCODE_MODELS"', scripts)
        self.assertNotIn('CONTINUUM_MODEL_STATE_TOKEN="$CONTINUUM_MODEL_STATE_TOKEN"', scripts)
        self.assertNotIn("receipt=", scripts)

    def test_private_child_ids_are_not_placed_in_workflow_environment(self):
        text, _ = self.load()
        self.assertNotIn("PARENT_CHILDREN", text)
        self.assertNotIn("PARENT_ROLE", text)
        self.assertNotIn("vars.CONTINUUM_CHILDREN", text)
        self.assertNotIn("vars.CONTINUUM_ROLE", text)

    def test_fresh_runner_is_read_only_and_does_not_rediscover(self):
        _, data = self.load()
        job = data["jobs"]["fresh-runner"]
        self.assertEqual(job["needs"], "provision-and-prove")
        scripts = "\n".join(
            step.get("run", "") for step in job["steps"] if isinstance(step, dict)
        )
        self.assertIn("private_model_receipt.py verify", scripts)
        for forbidden in (
            "delegation_repository.sh",
            "private_model_api.py",
            "private-child-map",
            "model_routing.py",
            "opencode run",
            "actions/variables",
            "gh api",
            "git/refs",
        ):
            self.assertNotIn(forbidden, scripts)
        verify_step = next(
            step
            for step in job["steps"]
            if isinstance(step, dict)
            and "private_model_receipt.py verify" in step.get("run", "")
        )
        self.assertEqual(
            verify_step["env"]["CONTINUUM_SEALED_RECEIPT"],
            "${{ needs.provision-and-prove.outputs.sealed }}",
        )

if __name__ == "__main__":
    unittest.main()
