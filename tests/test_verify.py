"""verify.sh med falske tjenester: den skal svare ærligt og rette det, der kan rettes."""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from hub import verifybots

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "verify.sh"
GOOD = [{"name": "Meridian AI", "configured": True, "up": True, "scan_age": 30, "trades": {"open": 5, "closed": 0}, "issue": "", "idle": ""},
        {"name": "Atlas AI", "configured": True, "up": True, "scan_age": None, "idle": "Førmarked. Åbner 15:30.", "issue": ""},
        {"name": "Nova AI", "configured": False, "up": False, "note_short": "Slået fra her", "issue": ""}]


class JudgeTests(unittest.TestCase):
    def test_every_state_is_judged_correctly(self):
        rows = GOOD + [{"name": "Orion AI", "configured": True, "up": False, "issue": "ZoneInfoNotFoundError"},
                       {"name": "Quasar AI", "configured": True, "up": True, "scan_age": None, "issue": "Ingen børs svarer"},
                       {"name": "Comet AI", "configured": True, "up": True, "scan_age": 5000, "issue": ""}]
        lines, bad = verifybots.judge(rows)
        self.assertEqual(bad, 3)                                                    # Orion, Quasar og Comet
        text = "\n".join(lines)
        self.assertIn("✓ Meridian AI: scanner", text)
        self.assertIn("✓ Atlas AI: venter på børsen", text)                         # sover med vilje: ikke et problem
        self.assertIn("– Nova AI: Slået fra her (tæller ikke med)", text)
        self.assertIn("✗ Orion AI: kører ikke – ZoneInfoNotFoundError", text)
        self.assertIn("✗ Quasar AI: kører, men scanner ikke – Ingen børs svarer", text)
        self.assertIn("ingen scanning set i 20 min", text)


@unittest.skipUnless(shutil.which("bash") and shutil.which("curl"), "bash/curl mangler")
class VerifyScriptTests(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)
        (self.d / "state").mkdir()
        self.log = self.d / "log"
        self.rows = list(GOOD)
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"ok" if self.path == "/healthz" else json.dumps(outer.rows).encode()
                self.send_response(200); self.end_headers(); self.wfile.write(body)

            def log_message(self, *a):
                pass
        self.srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.shutdown)
        self.port = self.srv.server_address[1]
        self.env_file = self.d / "nova-ai.env"
        self.write_env(HUB_PUBLIC="1", HUB_OPEN="1", HUB_PASSWORD="", HUB_TS_HTTPS="8443")
        # falsk systemctl: tilstand i filer (enabled-<enhed>, active-<enhed>); enable/restart/start sætter dem
        sc = self.d / "systemctl"
        sc.write_text(f'''#!/bin/bash
S="{self.d}/state"; echo "systemctl $*" >> "{self.log}"
cmd="$1"; shift
case "$cmd" in
  is-enabled) [ -e "$S/enabled-$1" ] && {{ echo enabled; exit 0; }}; echo disabled; exit 1 ;;
  is-active) [ "$1" = "--quiet" ] && shift; [ -e "$S/active-$1" ] ;;
  enable) [ "$1" = "--now" ] && {{ shift; touch "$S/active-$1"; }}; touch "$S/enabled-$1" ;;
  restart|start) touch "$S/active-$1" ;;
esac
''')
        sc.chmod(0o755)
        ts = self.d / "tailscale"
        ts.write_text(f'''#!/bin/bash
echo "tailscale $*" >> "{self.log}"
case "$1" in
  status) [ "$2" = "--json" ] && echo '{{"Self": {{"DNSName": "server.tail1.ts.net."}}}}'; exit 0 ;;
  funnel) [ "$2" = "status" ] && [ -e "{self.d}/state/funnel" ] && printf 'https://server.tail1.ts.net:8443 (Funnel on)\\n|-- / proxy http://127.0.0.1:8080\\n' ;;
esac
exit 0
''')
        ts.chmod(0o755)
        pub = self.d / "public.sh"                      # falsk public.sh: tæller kald og slår Funnel til
        pub.write_text(f'''#!/bin/bash
echo "public.sh $*" >> "{self.log}"
touch "{self.d}/state/funnel"
[ "$1" = open ] && printf 'HUB_PORT={self.port}\\nHUB_PUBLIC=1\\nHUB_OPEN=1\\nHUB_PASSWORD=\\nHUB_TS_HTTPS=8443\\n' > "{self.env_file}"
exit 0
''')
        pub.chmod(0o755)
        curl = self.d / "curl"                          # sender kald til URL'en på tailnettet videre til vores lokale server
        curl.write_text(f'#!/bin/bash\nargs=("$@"); out=()\nfor a in "${{args[@]}}"; do out+=("${{a/https:\\/\\/server.tail1.ts.net:8443/http://127.0.0.1:{self.port}}}"); done\nexec /usr/bin/env curl "${{out[@]}}"\n')
        curl.chmod(0o755)
        for unit in ("nova-ai", "nova-ai-update.timer", "tailscaled"):
            (self.d / "state" / f"enabled-{unit}").touch()
            (self.d / "state" / f"active-{unit}").touch()
        (self.d / "state" / "funnel").touch()
        (self.d / "nova-ai.service").write_text("[Service]\nRestart=always\n")

    def write_env(self, **kv):
        self.env_file.write_text(f"HUB_PORT={self.port}\n" + "".join(f"{k}={v}\n" for k, v in kv.items()))

    def run_script(self, *args, **extra):
        env = dict(os.environ, HUB_OS="Linux", HUB_SYSTEMCTL=str(self.d / "systemctl"), HUB_TAILSCALE=str(self.d / "ts") if False else str(self.d / "tailscale"),
                   HUB_ENV_FILE=str(self.env_file), HUB_UNIT_DIR=str(self.d), HUB_BOT_WAIT="0", HUB_PUBLIC_CMD=str(self.d / "public.sh"), HUB_CURL=str(self.d / "curl"), HUB_NO_SUDO="1", **extra)
        return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=240)

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_all_good_answers_yes_four_times_and_prints_the_address(self):
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.count("JA "), 4, r.stdout)
        self.assertIn("https://server.tail1.ts.net:8443", r.stdout)
        self.assertIn("?server=https://server.tail1.ts.net:8443", r.stdout)

    def test_a_stopped_disabled_hub_is_fixed(self):
        (self.d / "state" / "enabled-nova-ai").unlink()
        (self.d / "state" / "active-nova-ai").unlink()
        r = self.run_script()
        self.assertIn("systemctl enable nova-ai", self.calls())
        self.assertIn("systemctl restart nova-ai", self.calls())
        self.assertIn("(rettet)", r.stdout)
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_no_fix_mode_only_reports(self):
        (self.d / "state" / "enabled-nova-ai").unlink()
        r = self.run_script("--no-fix")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("NEJ", r.stdout)
        self.assertFalse(any(c.startswith("systemctl enable") or c.startswith("systemctl restart") for c in self.calls()), self.calls())

    def test_a_failing_bot_is_reported_with_its_reason_and_answered_no(self):
        self.rows = GOOD + [{"name": "Orion AI", "configured": True, "up": False, "issue": "tzdata mangler"}]
        r = self.run_script()
        self.assertIn("✗ Orion AI: kører ikke – tzdata mangler", r.stdout)
        self.assertIn("python3 -m hub report", r.stdout)
        self.assertIn("2. Alle botter kører uden problem:         NEJ", r.stdout)
        self.assertNotEqual(r.returncode, 0)

    def test_closed_access_is_opened_by_the_fix(self):
        self.write_env(HUB_PUBLIC="0", HUB_OPEN="0", HUB_PASSWORD="hemmeligt", HUB_TS_HTTPS="8443")
        r = self.run_script()
        self.assertIn("public.sh open --yes", self.calls())
        self.assertIn("Åbent for alle uden adgangskode", r.stdout)

    def test_a_missing_funnel_is_reported_with_the_two_admin_pages(self):
        (self.d / "state" / "funnel").unlink()
        r = self.run_script()
        self.assertIn("4. Alle kan komme ind, hvor de end er:     NEJ", r.stdout)
        self.assertIn("login.tailscale.com/admin/acls", r.stdout)


if __name__ == "__main__":
    unittest.main()
