"""public.sh med en falsk tailscale: hubben må aldrig tage en adresse, som en anden tjeneste (fx Nova) allerede bruger."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "public.sh"

STUB = r'''#!/bin/bash
echo "tailscale $*" >> "$STUBLOG"
# som på en rigtig server: ændringer afvises, medmindre de køres som root (falsk sudo sætter STUBROOT)
if { [ -n "${STUBNEEDROOT:-}" ] && [ -z "${STUBROOT:-}" ]; } || [ -n "${STUBALWAYSDENY:-}" ]; then case "$1 $2" in "serve --bg"|"funnel --bg"|"funnel --https"*|"serve --https"*|"funnel "[0-9]*|"serve https"*) echo "sending serve config: Access denied: serve config denied"; exit 1;; esac; fi
hp=443; for a in "$@"; do case "$a" in --https=*) hp="${a#--https=}";; esac; done
suffix=""; [ "$hp" != 443 ] && suffix=":$hp"
case "$1" in
  status) exit 0 ;;
  serve)
    if [ "$2" = "status" ]; then cat "$STUBSERVE" 2>/dev/null; exit 0; fi
    [ "$2" = "--bg" ] && printf 'Available within your tailnet:\n\nhttps://server.tail1.ts.net%s/\n' "$suffix" ;;
  funnel)
    [ "$2" = "status" ] && exit 0
    [ "$2" = "--bg" ] && printf 'Available on the internet:\n\nhttps://server.tail1.ts.net%s/\n' "$suffix" ;;
esac
exit 0
'''


@unittest.skipUnless(shutil.which("bash"), "bash mangler")
class PublicScriptTests(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)
        (self.d / "bin").mkdir()
        ts = self.d / "bin" / "tailscale"
        ts.write_text(STUB)
        ts.chmod(0o755)
        self.env_file = self.d / "nova-ai.env"
        self.env_file.write_text("HUB_HOST=0.0.0.0\nHUB_PORT=8080\nHUB_PASSWORD=\n")
        self.log = self.d / "log"
        self.serve = self.d / "serve"

    def run_script(self, *args, **extra):
        sudo = self.d / "bin" / "sudo"
        sudo.write_text('#!/bin/bash\necho "sudo $*" >> "$STUBLOG"\nSTUBROOT=1 exec "$@"\n')
        sudo.chmod(0o755)
        env = dict(os.environ, STUBLOG=str(self.log), STUBSERVE=str(self.serve), HUB_TAILSCALE=str(self.d / "bin" / "tailscale"),
                   HUB_DRY_RUN="1", HUB_ENV_FILE=str(self.env_file), HUB_OS="Linux", PATH=f"{self.d / 'bin'}:{os.environ['PATH']}", **extra)
        return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env)

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_a_free_main_address_is_used(self):
        r = self.run_script("open", "--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("tailscale funnel --bg --https=443 8080", self.calls())
        self.assertIn("HUB_TS_HTTPS=443", self.env_file.read_text())

    def test_novas_address_on_443_is_left_alone_and_the_hub_gets_8443(self):
        self.serve.write_text("https://server.tail1.ts.net (tailnet only)\n|-- / proxy http://127.0.0.1:8787\n")
        r = self.run_script("open", "--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("tailscale funnel --bg --https=8443 8080", self.calls())
        self.assertFalse(any("--https=443 off" in c for c in self.calls()), self.calls())            # rører aldrig Novas adresse
        self.assertIn("HUB_TS_HTTPS=8443", self.env_file.read_text())
        self.assertIn("https://server.tail1.ts.net:8443", r.stdout)                                    # den rigtige adresse til dashboardet
        self.assertIn("Den bliver ikke rørt", r.stdout)

    def test_private_mode_also_avoids_novas_address(self):
        self.serve.write_text("https://server.tail1.ts.net (tailnet only)\n|-- / proxy http://127.0.0.1:8787\n")
        r = self.run_script("private")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("tailscale serve --bg --https=8443 8080", self.calls())
        self.assertFalse(any("--https=443" in c for c in self.calls()), self.calls())

    def test_off_removes_only_the_hubs_own_address(self):
        self.serve.write_text("https://server.tail1.ts.net (tailnet only)\n|-- / proxy http://127.0.0.1:8787\n")
        self.run_script("open", "--yes")
        self.log.write_text("")
        r = self.run_script("off")
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.calls()
        self.assertTrue(any("--https=8443 off" in c for c in calls), calls)
        self.assertFalse(any("--https=443 off" in c for c in calls), calls)

    def test_off_without_a_stored_port_touches_nothing(self):
        r = self.run_script("off")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(any(" off" in c for c in self.calls()), self.calls())

    def test_tailscale_access_denied_is_retried_with_sudo(self):
        self.serve.write_text("https://server.tail1.ts.net (tailnet only)\n|-- / proxy http://127.0.0.1:8787\n")
        r = self.run_script("open", "--yes", STUBNEEDROOT="1")          # præcis fejlen fra din server
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("sudo " + str(self.d / "bin" / "tailscale") + " funnel --bg --https=8443 8080", self.calls())
        self.assertIn("https://server.tail1.ts.net:8443", r.stdout)
        self.assertNotIn("Access denied", r.stdout)

    def test_if_tailscale_still_refuses_with_sudo_the_help_says_what_to_do(self):
        self.serve.write_text("")
        r = self.run_script("open", "--yes", STUBALWAYSDENY="1")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("administratorrettigheder", r.stdout)
        self.assertIn("set --operator", r.stdout)
        self.assertNotIn("admin/acls", r.stdout)                       # ikke den forkerte hjælp om kontoindstillinger

    def test_the_hub_keeps_443_when_it_already_owns_it(self):
        self.serve.write_text("https://server.tail1.ts.net (Funnel on)\n|-- / proxy http://127.0.0.1:8080\n")
        r = self.run_script("on")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("tailscale funnel --bg --https=443 8080", self.calls())


if __name__ == "__main__":
    unittest.main()
