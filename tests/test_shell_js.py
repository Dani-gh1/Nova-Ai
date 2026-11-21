import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SHELL = Path(__file__).resolve().parent.parent / "hub" / "shell.html"


@unittest.skipUnless(shutil.which("node"), "kræver node")
class ShellScriptTests(unittest.TestCase):
    def test_dashboard_javascript_has_no_syntax_errors(self):
        """En syntaksfejl i dashboardet får siden til at stå og loade for altid (sket én gang: 'live' erklæret to gange)."""
        js = "\n;\n".join(re.findall(r"<script(?![^>]*src)[^>]*>(.*?)</script>", SHELL.read_text(encoding="utf-8"), re.S))
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(js)
        r = subprocess.run(["node", "--check", f.name], capture_output=True, text=True)
        Path(f.name).unlink()
        self.assertEqual(r.returncode, 0, r.stderr[-500:])


if __name__ == "__main__":
    unittest.main()
