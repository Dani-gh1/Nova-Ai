"""Port, vært og datamappe kan sættes udefra (bruges af hubben), og standarderne er uændrede."""
import importlib
import os
import unittest
from pathlib import Path
from unittest import mock

import config


class EnvConfigTests(unittest.TestCase):
    def reload(self, env):
        with mock.patch.dict(os.environ, env, clear=False):
            return importlib.reload(config)

    def tearDown(self):
        for k in ("DESK_PORT", "DESK_HOST", "DESK_DATA_DIR", "DESK_STATE_FILE"):
            os.environ.pop(k, None)
        importlib.reload(config)

    def test_defaults_are_unchanged(self):
        for k in ("DESK_PORT", "DESK_HOST", "DESK_DATA_DIR", "DESK_STATE_FILE"):
            os.environ.pop(k, None)
        c = importlib.reload(config)
        self.assertEqual((c.DASHBOARD_PORT, c.DASHBOARD_HOST), (8787, "0.0.0.0"))
        self.assertEqual(c.DATA_DIR, c.BASE_DIR / "data")
        self.assertEqual(c.STATE_FILE, c.BASE_DIR / "state.json")

    def test_environment_overrides(self):
        c = self.reload({"DESK_PORT": "8801", "DESK_HOST": "127.0.0.1", "DESK_DATA_DIR": "/tmp/x-data", "DESK_STATE_FILE": "/tmp/x-state.json"})
        self.assertEqual((c.DASHBOARD_PORT, c.DASHBOARD_HOST), (8801, "127.0.0.1"))
        self.assertEqual((c.DATA_DIR, c.STATE_FILE, c.DB_FILE), (Path("/tmp/x-data"), Path("/tmp/x-state.json"), Path("/tmp/x-data/desk.sqlite")))


if __name__ == "__main__":
    unittest.main()
