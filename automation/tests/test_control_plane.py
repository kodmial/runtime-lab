"""Guards the execution-backend cutover contract used by the scheduler.

The stable scheduler envelope (.github/workflows/issue-scheduler.yml) reads
``automation/control-plane.json`` from ``main`` and only accepts
``actions`` or ``render-controller``. Issue #12 cuts over by flipping that
single value, and issue #13 depends on it staying ``render-controller``.
A typo there would only fail at scheduler runtime after merge, so this
test fails fast locally and in ordinary CI instead.
"""

import json
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTROL_PLANE_PATH = REPO_ROOT / "automation" / "control-plane.json"
ALLOWED_BACKENDS = {"actions", "render-controller"}


def load_control_plane(path=CONTROL_PLANE_PATH):
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise AssertionError("control-plane.json must contain a JSON object")
    return data


class ControlPlaneContractTest(unittest.TestCase):
    def test_file_exists_and_is_valid_json(self):
        self.assertTrue(
            CONTROL_PLANE_PATH.is_file(),
            "automation/control-plane.json must exist",
        )
        self.assertIsInstance(load_control_plane(), dict)

    def test_execution_backend_is_supported(self):
        data = load_control_plane()
        backend = data.get("execution_backend")
        self.assertIn(
            backend,
            ALLOWED_BACKENDS,
            "execution_backend must be one of %s; got %r"
            % (sorted(ALLOWED_BACKENDS), backend),
        )

    def test_no_unexpected_top_level_keys(self):
        data = load_control_plane()
        self.assertEqual(
            set(data.keys()),
            {"execution_backend"},
            "control-plane.json must only define execution_backend "
            "until the controller schema is specified elsewhere",
        )


if __name__ == "__main__":
    unittest.main()
