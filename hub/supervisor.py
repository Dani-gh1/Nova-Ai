"""Vagthund: holder botterne kørende uden at nogen skal gøre noget.

* En bot, der går ned, startes igen efter en kort pause (5 s, derefter dobbelt så lang hver gang, højst 5 min).
  Har den kørt stabilt i 10 min, nulstilles pausen.
* En bot, hvis dashboard ikke svarer i 5 min, selv om processen lever (den hænger), bliver dræbt og startet igen.
* Er botterns port stadig optaget af en gammel proces, der er ved at lukke, venter vi i stedet for at fejle.
* En bot, der kører og svarer, men ikke har scannet markedet i 45 min. (fx fordi den sidder fast i et kald), genstartes.
  Kender vi ikke botens scanningstid (fx fordi børsen er spærret), genstartes den ikke, for det ville ikke hjælpe.
* Al output skrives til logs/<bot>.log (roteres ved 5 MB).
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

BACKOFF_START, BACKOFF_MAX, STABLE_AFTER = 5.0, 300.0, 600.0
HANG_AFTER = 300.0          # sekunder uden et svar på porten, før en levende proces regnes for hængt
PORT_WAIT = 150.0           # så længe venter vi på, at en gammel proces slipper porten
LOG_MAX = 5_000_000
HEALTH_EVERY = 60.0          # så ofte spørger vi, hvornår botten sidst scannede
STALL_AFTER = 45 * 60        # så længe uden scanning før en levende bot genstartes
FAST_FAIL = 120.0            # en bot, der stopper inden for så lang tid, regnes for et hurtigt nedbrud
HEAL_EVERY = 3               # efter så mange hurtige nedbrud i træk forsøger hubben at rette årsagen
GRACE = 10 * 60              # en nystartet bot får lov at komme i gang først


@dataclass
class Unit:
    key: str
    name: str
    cmd: list[str]
    cwd: Path
    port: int | None = None          # kun enheden, der ejer dashboard-porten, overvåges for hængning
    log_name: str | None = None      # navn på logfilen (standard: botnavnet)
    proc: subprocess.Popen | None = field(default=None, repr=False)
    started: float = 0.0
    last_ok: float = 0.0
    next_start: float = 0.0
    backoff: float = BACKOFF_START
    restarts: int = 0
    port_wait_since: float | None = None
    log_file: object = field(default=None, repr=False)
    health: object = field(default=None, repr=False)      # () -> sekunder siden seneste scanning (eller None)
    next_health: float = 0.0
    env: dict | None = field(default=None, repr=False)      # ekstra miljøvariabler til denne bot
    heal: object = field(default=None, repr=False)          # (forsøg) -> tekst: prøver at rette årsagen til gentagne nedbrud
    on_stable: object = field(default=None, repr=False)     # () -> None: kaldes, når botten har kørt stabilt
    fast_fails: int = 0
    heals: int = 0
    marked_good: bool = False


class Supervisor:
    def __init__(self, units: list[Unit], logdir: Path, port_open, popen=subprocess.Popen, now=time.monotonic, say=print, stagger: float = 0.0):
        self.units, self.logdir, self.port_open, self.popen, self.now, self.say = units, logdir, port_open, popen, now, say
        self.stagger, self.next_slot = stagger, 0.0       # start botterne et ad gangen, så en lille maskine ikke drukner
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None

    # ---------------------------------------------------------------- start/stop
    def _open_log(self, u: Unit):
        self.logdir.mkdir(parents=True, exist_ok=True)
        path = self.logdir / f"{u.log_name or u.key}.log"
        try:
            if path.exists() and path.stat().st_size > LOG_MAX:
                path.replace(path.with_suffix(".log.1"))
        except OSError:
            pass
        return open(path, "ab", buffering=0)

    def _start(self, u: Unit) -> None:
        if u.log_file:
            try:
                u.log_file.close()
            except OSError:
                pass
        u.log_file = self._open_log(u)
        u.log_file.write(f"\n--- start {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n".encode())
        kw = {"env": {**os.environ, **u.env}} if u.env else {}
        u.proc = self.popen(u.cmd, cwd=u.cwd, stdout=u.log_file, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **kw)
        u.started = u.last_ok = self.now()
        u.port_wait_since = None

    def tick(self) -> None:
        t = self.now()
        for u in self.units:
            if u.proc is None:
                if t < u.next_start or t < self.next_slot:
                    continue
                if u.port and self.port_open(u.port):                      # gammel proces holder stadig porten
                    u.port_wait_since = u.port_wait_since or t
                    if t - u.port_wait_since < PORT_WAIT:
                        u.next_start = t + 3
                        continue
                    self.say(f"! {u.name}: port {u.port} er stadig optaget efter {PORT_WAIT:.0f} s – starter alligevel")
                self._start(u)
                self.next_slot = t + self.stagger
                self.say(f"▶ {u.name} startet" + (f" (genstart nr. {u.restarts})" if u.restarts else ""))
                continue
            code = u.proc.poll()
            if code is not None:                                            # processen er død
                up = t - u.started
                if up >= STABLE_AFTER:
                    u.backoff, u.fast_fails, u.heals = BACKOFF_START, 0, 0
                elif up < FAST_FAIL:
                    u.fast_fails += 1
                u.marked_good = False
                wait, u.backoff = u.backoff, min(u.backoff * 2, BACKOFF_MAX)     # 5 s, 10 s, 20 s … højst 5 min
                u.proc, u.restarts, u.next_start = None, u.restarts + 1, t + wait
                self.say(f"✗ {u.name} stoppede (kode {code}) efter {up:.0f} s – starter igen om {wait:.0f} s")
                if u.heal and u.fast_fails and u.fast_fails % HEAL_EVERY == 0:      # bliver ved med at gå ned: ret årsagen
                    u.heals += 1
                    try:
                        self.say(f"🔧 {u.name} går ned igen og igen – prøver at rette det: {u.heal(u.heals)}")
                    except Exception as e:
                        self.say(f"! selvreparation af {u.name} fejlede: {type(e).__name__}: {e}")
                continue
            if u.on_stable and not u.marked_good and t - u.started >= STABLE_AFTER:
                u.marked_good = True
                try:
                    u.on_stable()
                except Exception:
                    pass
            if u.port:                                                      # processen lever: svarer dashboardet?
                if self.port_open(u.port):
                    u.last_ok = t
                elif t - u.last_ok > HANG_AFTER:
                    self.say(f"✗ {u.name} svarer ikke på port {u.port} i {HANG_AFTER:.0f} s – dræber og starter igen")
                    self._kill(u)
                    continue
                if u.health and t >= u.next_health and t - u.started > GRACE:      # … og scanner den stadig?
                    u.next_health = t + HEALTH_EVERY
                    try:
                        age = u.health()
                    except Exception:
                        age = None
                    if age is not None and age > STALL_AFTER:
                        self.say(f"✗ {u.name} har ikke scannet i {age / 60:.0f} min – dræber og starter igen")
                        self._kill(u)

    def _kill(self, u: Unit) -> None:
        if u.proc and u.proc.poll() is None:
            u.proc.terminate()
            try:
                u.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                u.proc.kill()
                u.proc.wait()
        u.proc = None
        u.restarts += 1
        u.next_start = self.now() + BACKOFF_START

    def run_forever(self) -> None:
        while not self.stop.wait(2.0):
            try:
                self.tick()
            except Exception as e:                                          # vagthunden må aldrig selv gå ned
                self.say(f"! vagthund-fejl: {type(e).__name__}: {e}")

    def start_thread(self) -> None:
        self.tick()
        self.thread = threading.Thread(target=self.run_forever, daemon=True, name="supervisor")
        self.thread.start()

    def shutdown(self, wait: float = 150.0) -> None:
        self.stop.set()
        # egne referencer: supervisor-tråden kan nå at nulstille u.proc, mens vi lukker
        live = [p for p in (u.proc for u in self.units) if p is not None and p.poll() is None]
        if live:
            self.say("Lukker botterne (kan tage op til to minutter, fordi nogle gør deres scanning færdig) …")
        for p in live:
            p.terminate()
        end = time.monotonic() + wait
        for p in live:
            try:
                p.wait(timeout=max(1.0, end - time.monotonic()))
            except subprocess.TimeoutExpired:
                p.kill()
        for u in self.units:
            u.proc = None
            if u.log_file:
                try:
                    u.log_file.close()
                except OSError:
                    pass

    def status(self) -> list[dict]:
        t = self.now()
        return [{"key": u.key, "alive": bool(u.proc and u.proc.poll() is None), "restarts": u.restarts,
                 "uptime_s": round(t - u.started) if u.proc else 0} for u in self.units]
