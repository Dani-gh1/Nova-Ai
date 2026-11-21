import os
import subprocess
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "migrate.sh"


def run(*args, **env):
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env={**os.environ, "HUB_DRY_RUN": "1", **env}, timeout=30)


class MigrateTests(unittest.TestCase):
    def test_dry_run_moves_everything_and_turns_all_bots_on(self):
        r = run("1.1.1.1", "2.2.2.2")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = r.stdout + r.stderr
        self.assertIn("Nova-AI etc/nova-ai.env", out)                      # kode, botter og indstillinger kopieres
        self.assertIn("--exclude='home/ubuntu/Nova-AI/backups'", out)
        for k in ("ATLASGLOBAL", "VALUTAAI", "REGNSKABAI"):
            self.assertIn(f"/^HUB_DISABLE_{k}=/d", out)                # de nye botter slås til på den store server
        self.assertIn("/^HUB_LITE=/d", out)                               # sparetilstand slås fra på den store server
        self.assertIn("[1.1.1.1] sudo systemctl stop nova-ai-update.timer nova-ai", out)
        self.assertIn("[1.1.1.1] sudo systemctl disable nova-ai", out)    # den gamle må ikke starte igen (to papirkonti)
        self.assertIn("./deploy/install.sh", out)

    def test_needs_two_different_servers(self):
        self.assertNotEqual(run().returncode, 0)
        self.assertNotEqual(run("1.1.1.1", "1.1.1.1").returncode, 0)

    def test_every_new_bot_is_in_the_list(self):
        import json, re
        bots = json.loads((SCRIPT.parent.parent / "bots.json").read_text(encoding="utf-8"))["bots"]
        listed = set(re.search(r'NEW_BOTS="([^"]+)"', SCRIPT.read_text(encoding="utf-8")).group(1).split())
        originals = {"ATLAS", "MERIDIAN", "COMET", "DELPHI", "QUASAR", "ORION", "NOVA"}
        self.assertEqual({b["key"].upper() for b in bots} - originals, listed)


if __name__ == "__main__":
    unittest.main()
