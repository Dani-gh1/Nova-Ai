import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hub import __main__ as cli


class EnvFileTests(unittest.TestCase):
    def test_hub_env_is_loaded_without_overriding_and_comments_are_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "nova-ai.env"
            f.write_text("# kommentar\nHUB_EXTERNAL_NOVA=8787\nHUB_PORT=8080   # port\n#OANDA_TOKEN=\nHUB_PASSWORD=\nHUB_KEEP=fra-filen\n")
            with mock.patch.dict(os.environ, {"HUB_ENV_FILE": str(f), "HUB_KEEP": "fra-miljøet"}, clear=False):
                for k in ("HUB_EXTERNAL_NOVA", "HUB_PORT", "HUB_PASSWORD", "OANDA_TOKEN"):
                    os.environ.pop(k, None)
                self.assertEqual(cli.load_env_file(), str(f))
                self.assertEqual(os.environ["HUB_EXTERNAL_NOVA"], "8787")
                self.assertEqual(os.environ["HUB_PORT"], "8080")                    # efterfølgende kommentar fjernes
                self.assertEqual(os.environ["HUB_PASSWORD"], "")
                self.assertNotIn("OANDA_TOKEN", os.environ)                          # udkommenterede linjer ignoreres
                self.assertEqual(os.environ["HUB_KEEP"], "fra-miljøet")              # eksisterende miljø vinder
                for k in ("HUB_EXTERNAL_NOVA", "HUB_PORT", "HUB_PASSWORD"):
                    os.environ.pop(k, None)

    def test_an_unreadable_or_missing_file_is_not_an_error(self):
        with mock.patch.dict(os.environ, {"HUB_ENV_FILE": "/findes/ikke/nova-ai.env"}):
            self.assertIsNone(cli.load_env_file())

    def test_status_explains_why_a_bot_is_down(self):
        import io
        from contextlib import redirect_stdout
        from hub import server
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "x.log").write_text("start\nzoneinfo._common.ZoneInfoNotFoundError: 'No time zone found with key America/New_York'\n")
            old = server.LOG_DIR
            server.LOG_DIR = Path(d)
            try:
                bots = [{"key": "x", "name": "X", "port": 1, "repo": "a/b"}, {"key": "y", "name": "Y", "port": 1, "repo": "a/c", "disabled": True}]
                out = io.StringIO()
                with redirect_stdout(out):
                    cli.cmd_status(bots)
            finally:
                server.LOG_DIR = old
        text = out.getvalue()
        self.assertIn("sidste fejl:", text)
        self.assertIn("ZoneInfoNotFoundError", text)
        self.assertEqual(text.count("sidste fejl"), 1)                               # den slåede fra bot forklares ikke


if __name__ == "__main__":
    unittest.main()
