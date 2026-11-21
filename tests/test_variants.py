import json
import subprocess
import sys
import unittest

from hub.__main__ import ROOT, commands
from hub.server import load_bots


class VariantTests(unittest.TestCase):
    def test_new_bots_have_own_ports_files_and_fees(self):
        by = {b["key"]: b for b in load_bots()}
        for k in ("atlasglobal", "orionmid", "novaclassic"):
            self.assertIn(k, by)
        self.assertEqual(by["nova"]["repo"], by["novaclassic"]["repo"])
        self.assertNotEqual(by["orion"]["port"], by["orionmid"]["port"])
        self.assertEqual(by["orionmid"]["launch"]["overrides"]["config"]["STATE_FILE"], "state-mid.json")
        self.assertTrue(all(b["fees"]["text"] for b in by.values()))

    def test_nova_classic_uses_its_own_config_file(self):
        cmd, _ = commands({b["key"]: b for b in load_bots()}["novaclassic"])[0]
        self.assertIn(str(ROOT / "variants" / "nova-classic.toml"), cmd)
        self.assertTrue((ROOT / "variants" / "nova-classic.toml").is_file())

    def test_override_prelude_adds_removes_and_replaces(self):
        import tempfile, textwrap, pathlib
        with tempfile.TemporaryDirectory() as d:
            pathlib.Path(d, "fakecfg.py").write_text("S = {'A'}\nL = [{'name': 'x'}, {'name': 'y'}]\nV = 1\n")
            b = {"port": 9, "launch": {"kind": "patch", "config": "fakecfg", "args": ["run"],
                                       "overrides": {"fakecfg": {"+S": ["B"], "-L": ["x"], "V": 2}}}}
            code = commands(b)[0][0][2].split("sys.argv")[0] + "print(sorted(c.S), [i['name'] for i in c.L], c.V, c.DASHBOARD_PORT)"
            out = subprocess.run([sys.executable, "-c", code], cwd=d, capture_output=True, text=True)
            self.assertEqual(out.stdout.strip(), "['A', 'B'] ['y'] 2 9", out.stderr)


class EveryVariantTests(unittest.TestCase):
    """Hver variant skal kunne få sine indstillinger lagt ind i den hentede botkode, uden at noget fejler (springes over uden kode)."""

    def test_overrides_apply_to_the_real_bot_code(self):
        import os
        from hub.__main__ import bot_dir
        checked = 0
        for b in load_bots():
            if "overrides" not in b.get("launch", {}) or not b.get("repo") or not bot_dir(b).exists():
                continue
            cmd, env = commands(b)[0]
            code = cmd[cmd.index("-c") + 1].split("sys.argv")[0] + "print('ok')"
            r = subprocess.run([sys.executable, "-c", code], cwd=bot_dir(b), capture_output=True, text=True, env={**os.environ, **env}, timeout=60)
            self.assertEqual(r.stdout.strip(), "ok", f"{b['key']}: {r.stderr[-300:]}")
            checked += 1
        if not checked:
            self.skipTest("botkoden er ikke hentet her")

    def test_every_bot_has_unique_state_files(self):
        seen = {}
        for b in load_bots():
            ln = b.get("launch", {})
            files = [ln.get("env", {}).get("DESK_STATE_FILE"), ln.get("env", {}).get("DESK_DATA_DIR"),
                     (ln.get("overrides", {}).get("config") or {}).get("STATE_FILE")]
            for f in filter(None, files):
                self.assertNotIn((b["repo"], f), seen, f"{b['key']} deler fil med {seen.get((b['repo'], f))}")
                seen[(b["repo"], f)] = b["key"]


class LiteTests(unittest.TestCase):
    def test_lite_mode_scans_less_often_and_new_bots_run_with_low_priority(self):
        import os
        by = {b["key"]: b for b in load_bots()}
        os.environ["HUB_LITE"] = "1"
        try:
            lite = commands(by["bondai"])[0][0]
        finally:
            del os.environ["HUB_LITE"]
        normal = commands(by["bondai"])[0][0]
        self.assertEqual(lite[:3], ["nice", "-n", "10"])
        self.assertIn("'SCAN_INTERVAL_MINUTES': 30", lite[-1])
        self.assertNotIn("SCAN_INTERVAL_MINUTES", normal[-1])
        self.assertNotEqual(commands(by["atlas"])[0][0][0], "nice")         # de oprindelige botter får fuld prioritet


if __name__ == "__main__":
    unittest.main()
