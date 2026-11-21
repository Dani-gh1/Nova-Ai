"""Python fra python.org på en Mac leveres uden rod-certifikater, så alle HTTPS-kald fejler med
"unable to get local issuer certificate" (eller "self-signed certificate in certificate chain"). Det er ikke netværket.

Her bygger vi en certifikatfil ud fra Macens egen nøglering (som Safari bruger) og sætter SSL_CERT_FILE, før botterne
starter. Botterne arver den, og så virker alle kald, uden at nogen skal køre "Install Certificates.command".
"""
from __future__ import annotations

import os
import ssl
import subprocess
import sys
import time
from pathlib import Path

KEYCHAINS = ("/System/Library/Keychains/SystemRootCertificates.keychain", "/Library/Keychains/System.keychain")
MAX_AGE_S = 30 * 86400
MIN_BYTES = 20_000                 # en rigtig samling (flere hundrede rodcertifikater) fylder langt mere


def needs_fix() -> bool:
    """Har Python ingen brugbare rod-certifikater, og er der ikke sat en certifikatfil udefra?"""
    if os.environ.get("SSL_CERT_FILE") and Path(os.environ["SSL_CERT_FILE"]).is_file():
        return False
    p = ssl.get_default_verify_paths()
    if p.cafile and Path(p.cafile).is_file():
        return False
    if p.capath and Path(p.capath).is_dir() and any(Path(p.capath).iterdir()):
        return False
    return True


def build_pem(run=subprocess.run) -> str:
    out = []
    for kc in KEYCHAINS:
        try:
            r = run(["security", "find-certificate", "-a", "-p", kc], capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode == 0 and "BEGIN CERTIFICATE" in r.stdout:
            out.append(r.stdout)
    return "\n".join(out)


def conf_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Nova AI"
    return Path.home() / ".hub-ai"


def setup(directory: Path | None = None, say=print, run=subprocess.run, now=time.time) -> str | None:
    """Sætter SSL_CERT_FILE, hvis det er nødvendigt og muligt. Returnerer filens sti, ellers None."""
    if not needs_fix():
        return None
    d = directory or conf_dir()
    pem = d / "cacert.pem"
    try:
        if not (pem.is_file() and pem.stat().st_size > MIN_BYTES and now() - pem.stat().st_mtime < MAX_AGE_S):
            text = build_pem(run)
            if len(text) < MIN_BYTES:
                say("! Pythons rod-certifikater mangler, og Macens nøglering kunne ikke læses. Kør Install Certificates.command til din Python.")
                return None
            d.mkdir(parents=True, exist_ok=True)
            pem.write_text(text)
    except OSError as e:
        say(f"! Kunne ikke gemme certifikatfilen ({e})")
        return None
    os.environ["SSL_CERT_FILE"] = str(pem)
    say("• Pythons rod-certifikater mangler (typisk for Python fra python.org på en Mac). Bruger Macens egne, så botterne kan hente data.")
    return str(pem)
