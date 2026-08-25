#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ssh_matrix.py - Testet fuer alle geordneten IP-Paare, ob Quelle A per SSH-Login
Ziel B erreichen kann (gleiche Credentials ueberall).

Ablauf:
  1. Kali -> A  : paramiko (User/Passwort) - persistente Verbindung pro Quelle,
                  wird fuer alle Ziele der Quelle wiederverwendet.
  2. A   -> B   : Fallback-Kette auf A:
                    sshpass  (falls auf A vorhanden)
                    sonst ssh + SSH_ASKPASS-Trick (askpass-Skript in /tmp | /dev/shm)
                    sonst nc (nur Port-Check)
                    sonst bash /dev/tcp (nur Port-Check)
                    sonst Status no_tool
  3. Ergebnis je Richtung als Zeile in detail.csv (inkrementell, crash-sicher).

Resume: --resume ueberspringt bereits getestete Paare.

Beide Richtungen entstehen automatisch, weil alle geordneten Paare
(A != B) getestet werden: A->B und B->A sind zwei eigene Tests.
"""

import argparse
import csv
import ipaddress
import logging
import os
import queue
import re
import secrets
import shlex
import socket
import sys
import threading
import time
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone

try:
    import paramiko
except ImportError:
    print("FEHLER: paramiko fehlt. Auf dem Kali-Host installieren:", file=sys.stderr)
    print("  sudo apt install -y python3-paramiko", file=sys.stderr)
    sys.exit(2)

DEFAULT_PORT = 22
TOOL_PROBE = "sshpass ssh nc bash setsid timeout sh base64"

# Gueltige Statuswerte (fuer --retry-status Validierung).
KNOWN_STATUSES = {
    "auth_ok", "auth_fail", "port_open", "port_closed", "net_unreachable",
    "source_unreachable", "no_tool", "tool_error", "unclear", "skipped",
}
# Status, die bei --retry-all-failed NICHT neu getestet werden
# (volle Erfolge + skipped = nicht getestet, kein Fehler).
RETRY_ALL_EXCLUDE = {"auth_ok", "skipped"}
SUCCESS_STATUSES = {"auth_ok"}

VERSION = "v2.1.1"
AUTHOR = "Alex & DeepSeek"

# RAM-Warnschwelle in MB (per env ueberschreibbar, z.B. fuer Tests).
RAM_WARN_MB = int(os.environ.get("SSH_MATRIX_RAM_WARN_MB", "1024"))


def parse_duration(s: str) -> int:
    """Parst Dauer wie '300', '5m', '2h', '30s' -> Sekunden (int)."""
    s = s.strip().lower()
    if not s:
        raise argparse.ArgumentTypeError("leere Dauer")
    m = re.fullmatch(r"(\d+)([smh]?)", s)
    if not m:
        raise argparse.ArgumentTypeError(
            f"ungueltige Dauer {s!r}, erwartet Zahl + optional s/m/h (z.B. 300, 5m, 2h)")
    val = int(m.group(1))
    unit = m.group(2) or "s"
    if unit == "s":
        return val
    if unit == "m":
        return val * 60
    if unit == "h":
        return val * 3600
    raise argparse.ArgumentTypeError(f"ungueltige Einheit {unit!r}")


def estimate_ram_mb(n: int) -> float:
    """Grobe Schaetzung des Spitzen-RAM in MB. Streaming-Architektur:
    kein O(n^2)-Speicher mehr (Bitmaps statt pairs-Liste/done-Set)."""
    mb = 60.0                 # Python + paramiko + App-Basis
    mb += n * 0.002           # hosts + id-Map
    mb += (n * n) / 8 / 1e6   # done-Bitmap (1 Bit/Paar)
    return mb


def print_banner(stream=sys.stderr) -> None:
    """Start-Banner mit Version und Autoren-Hinweis (farbig bei TTY)."""
    line = "=" * 44
    if USE_COLOR:
        print(paint(C_CYAN, line, bold=True), file=stream)
        print(paint(C_CYAN, f"   SSH-Matrix-Tester {VERSION}", bold=True), file=stream)
        print(paint(C_CYAN, f"   entwickelt von {AUTHOR}", bold=True), file=stream)
        print(paint(C_CYAN, line, bold=True), file=stream)
    else:
        print(line, file=stream)
        print(f"   SSH-Matrix-Tester {VERSION}", file=stream)
        print(f"   entwickelt von {AUTHOR}", file=stream)
        print(line, file=stream)

# Quota-Modi: welche Testergebnisse zaehlen als "funktionierender Quell-Host"
# fuer --subnet-quota.
#   auth_ok   : nur voller SSH-Login
#   reachable : Netzwerkseitig erreichbar (Login ok ODER Login abgelehnt ODER
#               nur Port offen) - beweist, dass der Weg zum Ziel-Netz funktioniert
QUOTA_MODES = {
    "auth_ok": {"auth_ok"},
    "reachable": {"auth_ok", "auth_fail", "port_open"},
}

# --verbose Werte -> logging-Level (stderr; run.log bleibt immer INFO).
LOG_LEVELS = {
    "err": logging.ERROR,
    "warn": logging.WARNING,
    "info": logging.INFO,
}

# Klartext-Beschreibungen der Status (fuer den Zwischenbericht).
STATUS_DESCRIPTIONS = {
    "auth_ok": "Volle SSH-Logins erfolgreich - Quelle A konnte sich auf Ziel B anmelden",
    "auth_fail": "Login abgelehnt - SSH-Port offen, aber Passwort/User falsch",
    "port_open": "Nur Netzwerkport erreichbar - kein Login getestet, nur TCP offen",
    "port_closed": "Port zu - Dienst laeuft nicht auf dem Ziel (Connection refused)",
    "net_unreachable": "Netzwerk nicht erreichbar - Timeout, Firewall oder No Route",
    "source_unreachable": "Quelle vom Kali nicht erreichbar - Kali->A fehlgeschlagen",
    "no_tool": "Kein Testwerkzeug auf der Quelle - ssh/nc/bash fehlen",
    "tool_error": "Unerwarteter Fehler beim Test - error-Spalte in detail.csv pruefen",
    "unclear": "Nicht eindeutig - Port zu oder gefiltert",
    "skipped": "Uebersprungen - Subnetz-Quota fuer diese Richtung erfuellt",
}


class RunConfig:
    """Geteilte, zur Laufzeit veraenderbare Laufparameter.

    Worker lesen timeout/subnet_quota/quota_mode pro Test neu - Aenderungen
    aus dem Pause-Menue wirken sofort auf noch nicht getestete Paare.
    """

    def __init__(self, args):
        self.lock = threading.Lock()
        self.timeout = args.timeout
        self.subnet_quota = args.subnet_quota
        self.quota_mode = args.quota_mode
        self.target_workers = args.workers
        self.active_workers = 0
        self.verbose_level = args.verbose
        self.stream_handler = None  # stderr-Handler (fuer --verbose zur Laufzeit)
        # pause-and-retry-when-auth-failed
        self.auth_pause = getattr(args, "auth_pause", 0)
        self.auth_pause_threshold = getattr(args, "auth_pause_threshold", 3)
        self.auth_pause_window = getattr(args, "auth_pause_window", 60)
        self.auth_pause_retries = getattr(args, "auth_pause_retries", 1)
        self._auth_fail_times: deque = deque()
        self._pause_until: float = 0.0
        self._last_pause: float = 0.0

    @property
    def quota_statuses(self) -> set:
        return QUOTA_MODES[self.quota_mode]

    def is_paused(self) -> bool:
        with self.lock:
            return time.monotonic() < self._pause_until

    def wait_if_paused(self, stop_event: threading.Event) -> None:
        while True:
            with self.lock:
                until = self._pause_until
            now = time.monotonic()
            if until <= now:
                return
            remaining = until - now
            if stop_event.wait(min(remaining, 0.5)):
                return

    def record_auth_fail(self, stop_event: threading.Event, log) -> bool:
        """Registriert einen auth_fail. Liefert True wenn globale Pause ausgeloest."""
        if self.auth_pause <= 0:
            return False
        triggered = False
        with self.lock:
            now = time.monotonic()
            while self._auth_fail_times and self._auth_fail_times[0] < now - self.auth_pause_window:
                self._auth_fail_times.popleft()
            self._auth_fail_times.append(now)
            if len(self._auth_fail_times) < self.auth_pause_threshold:
                if now < self._pause_until:
                    triggered = False
                else:
                    return False
            else:
                if now < self._last_pause + self.auth_pause_window + self.auth_pause:
                    return False
                self._last_pause = now
                self._pause_until = now + self.auth_pause
                triggered = True
                self._auth_fail_times.clear()
        if triggered:
            log.warning("Auth-Fail Block erkannt (%d fails in %ds) - pausiere %ds ...",
                        self.auth_pause_threshold, self.auth_pause_window, self.auth_pause)
            end = time.monotonic() + self.auth_pause
            while time.monotonic() < end:
                if stop_event.is_set():
                    break
                time.sleep(0.5)
            log.warning("Pause beendet - retried auths")
            return True
        else:
            self.wait_if_paused(stop_event)
            return False

# ------------------------------------------------------------ ANSI-Farben
USE_COLOR = sys.stderr.isatty() and not os.environ.get("NO_COLOR")

C_RESET = "\033[0m"
C_BOLD = "\033[1m"
C_GREEN = "\033[32m"
C_YELLOW = "\033[33m"
C_RED = "\033[31m"
C_BLUE = "\033[34m"
C_CYAN = "\033[36m"
C_GRAY = "\033[90m"

STATUS_ORDER = [
    "auth_ok", "auth_fail", "port_open", "port_closed",
    "net_unreachable", "source_unreachable", "no_tool", "tool_error",
    "unclear", "skipped",
]
STATUS_SHORT = {
    "auth_ok": "OK", "auth_fail": "AUTH", "port_open": "PORT",
    "port_closed": "CLOSED", "net_unreachable": "UNREACH",
    "source_unreachable": "SRCERR", "no_tool": "NOTOOL",
    "tool_error": "ERR", "unclear": "?", "skipped": "SKIP",
}
STATUS_COLORS = {
    "auth_ok": C_GREEN,
    "auth_fail": C_YELLOW,
    "port_open": C_BLUE,
    "port_closed": C_GRAY,
    "net_unreachable": C_RED,
    "source_unreachable": C_RED,
    "no_tool": C_YELLOW,
    "tool_error": C_RED,
    "unclear": C_GRAY,
    "skipped": C_GRAY,
}

# Textual-Markup-Tags je Status (fuer den TUI-Zwischenbericht).
STATUS_MARKUP = {
    "auth_ok": "green",
    "auth_fail": "yellow",
    "port_open": "blue",
    "port_closed": "dim",
    "net_unreachable": "red",
    "source_unreachable": "red",
    "no_tool": "yellow",
    "tool_error": "red",
    "unclear": "dim",
    "skipped": "dim",
}


def colored(status: str, text: str) -> str:
    if not USE_COLOR:
        return text
    col = STATUS_COLORS.get(status, "")
    return f"{col}{text}{C_RESET}" if col else text


def paint(color: str, text: str, bold: bool = False) -> str:
    """Farbe explizit waehlen (nur bei TTY). Keine ANSI-Codes in Pipes."""
    if not USE_COLOR:
        return text
    start = f"{C_BOLD}{color}" if bold else color
    return f"{start}{text}{C_RESET}"


def fmt_duration(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}min"
    if m:
        return f"{m}min {sec:02d}s"
    return f"{sec}s"


class ColorFormatter(logging.Formatter):
    """Farbt stderr-Logzeilen: verbunden=gruen, Warnung=gelb,
    nicht erreichbar=rot, Fehler=rot+fett. run.log bleibt ungefarbt."""

    def format(self, record):
        msg = logging.Formatter.format(self, record)
        if not USE_COLOR:
            return msg
        if record.levelno >= logging.ERROR:
            return f"{C_RED}{C_BOLD}{msg}{C_RESET}"
        if record.levelno >= logging.WARNING:
            if "nicht erreichbar" in msg:
                return f"{C_RED}{msg}{C_RESET}"
            return f"{C_YELLOW}{msg}{C_RESET}"
        if "verbunden" in msg:
            return f"{C_GREEN}{msg}{C_RESET}"
        if "Start:" in msg:
            return f"{C_CYAN}{msg}{C_RESET}"
        return msg

CSV_HEADER = [
    "timestamp", "source_ip", "source_port", "source_label",
    "src_24", "src_16", "src_net",
    "target_ip", "target_port", "target_label",
    "tgt_24", "tgt_16", "tgt_net",
    "direction", "method", "status", "latency_ms", "error",
]

IP_RE = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3})(?::(\d{1,5}))?\s*(?:#\s*(.*))?$")
CIDR_RE = re.compile(
    r"^(\d{1,3}(?:\.\d{1,3}){3})/(\d{1,2})(?::(\d{1,5}))?\s*(?:#\s*(.*))?$"
)
RANGE_RE = re.compile(
    r"^(\d{1,3}(?:\.\d{1,3}){3})-(\d{1,3}(?:\.\d{1,3}){3})(?::(\d{1,5}))?\s*(?:#\s*(.*))?$"
)


# ---------------------------------------------------------------- IP-Liste

def mkhost(ip: str, port: int, label: str) -> dict:
    return {
        "ip": ip,
        "port": port,
        "label": (label or "").strip(),
        "n24": ".".join(ip.split(".")[:3]) + ".0/24",
        "n16": ".".join(ip.split(".")[:2]) + ".0.0/16",
        "net": ".".join(ip.split(".")[:3]) + ".0/24",  # Default; wird durch cluster_subnets ersetzt
    }


def _common_prefix_len(a: int, b: int) -> int:
    """Gemeinsame Praefix-Laenge zweier IPv4-Adressen (als int)."""
    x = a ^ b
    return 32 - x.bit_length() if x else 32


def cluster_subnets(hosts: list, gap: int) -> None:
    """Gruppiert Hosts anhand von IP-Luecken (gap > Schwellwert) und weist
    jedem Host ein 'net'-Feld (kleinstes CIDR des Clusters) zu.
    gap == 0 -> feste /24 (net = n24, abwaertskompatibel)."""
    if not hosts:
        return
    if gap == 0:
        for h in hosts:
            h["net"] = h["n24"]
        return
    unique_ips = sorted(set(h["ip"] for h in hosts),
                       key=lambda ip: ipaddress.IPv4Address(ip).packed)
    clusters = []
    current = [unique_ips[0]]
    for ip in unique_ips[1:]:
        prev = int(ipaddress.IPv4Address(current[-1]))
        cur = int(ipaddress.IPv4Address(ip))
        if cur - prev > gap:
            clusters.append(current)
            current = [ip]
        else:
            current.append(ip)
    clusters.append(current)
    ip_to_net = {}
    for cluster in clusters:
        first = int(ipaddress.IPv4Address(cluster[0]))
        last = int(ipaddress.IPv4Address(cluster[-1]))
        pfx = _common_prefix_len(first, last)
        net = str(ipaddress.IPv4Network(f"{cluster[0]}/{pfx}", strict=False))
        for ip in cluster:
            ip_to_net[ip] = net
    for h in hosts:
        h["net"] = ip_to_net[h["ip"]]


def _valid_port(port_s: str, default_port: int) -> int | None:
    port = int(port_s) if port_s else default_port
    if not (1 <= port <= 65535):
        return None
    return port


def parse_endpoint(line: str, default_port: int) -> list | None:
    """Eine Zeile -> Liste von Host-Dicts (CIDR/Range expandieren)."""
    m = CIDR_RE.match(line)
    if m:
        ip_s, mask_s, port_s, label = m.groups()
        try:
            net = ipaddress.IPv4Network(f"{ip_s}/{mask_s}", strict=False)
            port = _valid_port(port_s, default_port)
            if port is None:
                return None
        except ValueError:
            return None
        return [mkhost(str(a), port, label) for a in net.hosts()]

    m = RANGE_RE.match(line)
    if m:
        a_s, b_s, port_s, label = m.groups()
        try:
            a = int(ipaddress.IPv4Address(a_s))
            b = int(ipaddress.IPv4Address(b_s))
            if b < a:
                return None
            port = _valid_port(port_s, default_port)
            if port is None:
                return None
        except ValueError:
            return None
        return [
            mkhost(str(ipaddress.IPv4Address(i)), port, label)
            for i in range(a, b + 1)
        ]

    m = IP_RE.match(line)
    if m:
        ip_s, port_s, label = m.groups()
        try:
            ipaddress.IPv4Address(ip_s)
            port = _valid_port(port_s, default_port)
            if port is None:
                return None
        except ValueError:
            return None
        return [mkhost(ip_s, port, label)]

    return None


def parse_ips_file(path: str, default_port: int, log) -> list:
    hosts: list = []
    seen: set = set()
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            expanded = parse_endpoint(line, default_port)
            if expanded is None:
                log.warning("Uebersprungen (ungueltige Zeile): %r", line)
                continue
            for h in expanded:
                key = (h["ip"], h["port"])
                if key in seen:
                    continue
                seen.add(key)
                hosts.append(h)
    hosts.sort(key=lambda h: (ipaddress.IPv4Address(h["ip"]).packed, h["port"]))
    return hosts


def ep_label(host: dict) -> str:
    return host["ip"] if host["port"] == DEFAULT_PORT else f"{host['ip']}:{host['port']}"


# ---------------------------------------------------------------- Quelle-Tester

class SourceTester:
    """Persistente SSH-Verbindung Kali -> Quelle A + A->B-Testlogik."""

    # Timeout fuer Channel-Open (paramiko-Default waere 3600 s = 1 h).
    OPEN_SESSION_TIMEOUT = 30

    def __init__(self, src: dict, user: str, password: str, config: RunConfig):
        self.src = src
        self.user = user
        self.password = password
        self.config = config
        self.client = None
        self.tools = None
        self.ssh_askpass_force = False
        self.askpass_path = None

    @property
    def timeout(self) -> int:
        """Timeout dynamisch aus config lesen - Aenderungen im Pause-Menue
        wirken sofort auf neue Tests/Connects."""
        return self.config.timeout

    # -- Verbindung ----------------------------------------------------

    def connect(self) -> tuple[bool, str]:
        for _ in (1, 2):
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            try:
                client.connect(
                    hostname=self.src["ip"],
                    port=self.src["port"],
                    username=self.user,
                    password=self.password,
                    timeout=self.timeout,
                    banner_timeout=self.timeout,
                    auth_timeout=self.timeout,
                    allow_agent=False,
                    look_for_keys=False,
                )
                transport = client.get_transport()
                if transport is not None:
                    # Tote/halboffene Verbindungen werden erkannt statt
                    # unbegrenzt zu haengen.
                    transport.set_keepalive(10)
                self.client = client
                return True, ""
            except paramiko.AuthenticationException as exc:
                # Diagnose: welche Auth-Methoden erlaubt der Server?
                allowed = getattr(exc, "allowed_types", None)
                banner = ""
                remote_version = ""
                try:
                    t = client.get_transport()
                    if t is not None:
                        remote_version = t.remote_version or ""
                        # banner_timeout/auth_timeout liefern oft die Ursache
                        banner = remote_version
                except Exception:
                    pass
                # Fallback: wenn Server nur keyboard-interactive erlaubt, mit dumb-handler probieren
                if allowed and "keyboard-interactive" in [a.strip() for a in (allowed or [])]:
                    try:
                        t = client.get_transport()
                        if t is not None and not t.is_authenticated():
                            t.auth_interactive_dumb(self.user)
                            # Einige Server erwarten hier das Passwort als Antwort
                            # paramiko 2.12: auth_interactive_dumb ohne handler nutzt
                            # internen Handler der [password] zurückgibt, wenn password gesetzt
                            # Fallback: explizit mit handler
                            if not t.is_authenticated():
                                t.auth_interactive(self.user,
                                    lambda title, instr, prompts: [self.password] * len(prompts))
                            if t.is_authenticated():
                                if t is not None:
                                    t.set_keepalive(10)
                                self.client = client
                                logging.getLogger("ssh_matrix").info(
                                    "Auth ok via keyboard-interactive (Fallback) auf %s:%s "
                                    "(server erlaubte: %s)",
                                    self.src["ip"], self.src["port"], allowed)
                                return True, ""
                    except Exception as ki_exc:
                        last = f"{exc} (keyboard-interactive Fallback: {ki_exc} allowed={allowed})"
                    else:
                        last = f"{exc} (allowed={allowed} banner={banner!r})"
                else:
                    last = f"{exc} (allowed={allowed} banner={banner!r})"
                logging.getLogger("ssh_matrix").warning(
                    "Auth fail %s@%s:%s - %s", self.user, self.src["ip"],
                    self.src["port"], last)
                try:
                    client.close()
                except Exception:
                    pass
                time.sleep(1)
            except (socket.timeout, paramiko.SSHException, OSError) as exc:
                last = str(exc)
                # Banner/SSHException mit Remote-Version anreichern
                try:
                    t = client.get_transport()
                    rv = t.remote_version if t else ""
                    if rv:
                        last = f"{exc} (banner={rv!r})"
                except Exception:
                    pass
                try:
                    client.close()
                except Exception:
                    pass
                time.sleep(1)
        return False, last

    def reconnect(self) -> tuple[bool, str]:
        self.cleanup()
        return self.connect()

    def cleanup(self) -> None:
        if self.askpass_path:
            try:
                self.run("rm -f " + shlex.quote(self.askpass_path), 10)
            except Exception:
                pass
            self.askpass_path = None
        if self.client is not None:
            try:
                self.client.close()
            except Exception:
                pass
            self.client = None

    # -- Kommando auf A ------------------------------------------------

    def run(self, cmd: str, timeout: float) -> tuple[str, str, int]:
        if self.client is None or self.client.get_transport() is None:
            return "", "connection closed", -1
        try:
            # open_session OHNE Timeout blockiert in paramiko bis zu 3600 s
            # (open_channel-Default), wenn der Server den Channel-Open-Request
            # nie bestaetigt (MaxSessions, Firewall, Ueberlast). Deshalb
            # explizit begrenzen - sonst haengt der Worker scheinbar endlos.
            t0 = time.monotonic()
            chan = self.client.get_transport().open_session(
                timeout=self.OPEN_SESSION_TIMEOUT)
            dt = time.monotonic() - t0
            if dt > 5:
                logging.getLogger("ssh_matrix").warning(
                    "Channel-Open langsam auf %s:%s: %.1fs",
                    self.src["ip"], self.src["port"], dt)
        except Exception as exc:
            return "", f"open_session: {exc}", -1
        chan.settimeout(1)

        # exec_command blockiert in paramiko 2.12 UNBEGRENZT
        # (_wait_for_event -> event.wait() ohne Timeout). Daher exec in einem
        # Helper-Thread mit join-Timeout ausfuehren, damit ein haengender
        # Server den Worker nicht dauerhaft blockiert.
        exec_err: list = []
        exec_done = threading.Event()

        def _exec():
            try:
                chan.exec_command(cmd)
            except Exception as exc:
                exec_err.append(exc)
            finally:
                exec_done.set()

        t = threading.Thread(target=_exec, daemon=True)
        t.start()
        if not exec_done.wait(timeout=30):
            try:
                chan.close()
            except Exception:
                pass
            return "", "exec_command timeout (keine Antwort vom Server)", -1
        if exec_err:
            try:
                chan.close()
            except Exception:
                pass
            return "", str(exec_err[0]), -1

        out: list = []
        err: list = []
        deadline = time.monotonic() + timeout
        exited = False
        while time.monotonic() < deadline:
            if chan.recv_ready():
                try:
                    data = chan.recv(65536)
                except Exception:
                    break
                if data:
                    out.append(data.decode("utf-8", "replace"))
            if chan.recv_stderr_ready():
                try:
                    data = chan.recv_stderr(65536)
                except Exception:
                    break
                if data:
                    err.append(data.decode("utf-8", "replace"))
            if chan.exit_status_ready():
                exited = True
                break
            time.sleep(0.05)

        # Nach Exit bis EOF lesen (Grace-Fenster, Timeouts werden toleriert
        # statt sofort abzubrechen - verhindert Verlust von Rest-Ausgabe).
        drain_deadline = time.monotonic() + 2.0
        while True:
            try:
                data = chan.recv(65536)
            except Exception:
                if time.monotonic() > drain_deadline:
                    break
                time.sleep(0.1)
                continue
            if not data:
                break
            out.append(data.decode("utf-8", "replace"))
        drain_deadline = time.monotonic() + 2.0
        while True:
            try:
                data = chan.recv_stderr(65536)
            except Exception:
                if time.monotonic() > drain_deadline:
                    break
                time.sleep(0.1)
                continue
            if not data:
                break
            err.append(data.decode("utf-8", "replace"))

        if exited:
            try:
                rc = chan.recv_exit_status()
            except Exception:
                rc = -1
        else:
            rc = -1
        try:
            chan.close()
        except Exception:
            pass
        return "".join(out), "".join(err), rc

    # -- Tool-Erkennung auf A -------------------------------------------

    def detect_tools(self) -> dict:
        if self.tools is not None:
            return self.tools
        probe = "; ".join(
            f"command -v {t} >/dev/null 2>&1 && echo HAVE:{t}" for t in TOOL_PROBE.split()
        )
        probe += "; ssh -V 2>&1"
        text = ""
        for _attempt in (1, 2, 3):
            out, err, _rc = self.run(probe, 15)
            text = out + "\n" + err
            if any(line.startswith("HAVE:") for line in text.splitlines()):
                break
            time.sleep(0.5)
        if not any(line.startswith("HAVE:") for line in text.splitlines()):
            # Nach 3 direkten Fehlversuchen CACHEN (nicht pro Target erneut
            # proben - das waere ~90s pro Target). Transiente Ausfaelle sind
            # durch die 3 Versuche abgedeckt. WARNING ins run.log.
            self.tools = {}
            logging.getLogger("ssh_matrix").warning(
                "Tool-Erkennung auf %s:%s nach 3 Versuchen ohne Ergebnis "
                "(Probe-Ausgabe: %.160r)",
                self.src["ip"], self.src["port"], text[:160])
            return self.tools
        lines = text.splitlines()
        self.tools = {name: (f"HAVE:{name}" in lines) for name in TOOL_PROBE.split()}
        m = re.search(r"OpenSSH[_-](\d+)\.(\d+)", text)
        self.ssh_askpass_force = bool(m) and (int(m.group(1)), int(m.group(2))) >= (8, 4)
        return self.tools

    def _ensure_askpass(self) -> str | None:
        """Askpass-Skript auf A anlegen (liest Passwort aus Env __AP, enthaelt
        es NICHT selbst). /tmp bevorzugt, /dev/shm als Fallback. Wird in
        cleanup() entfernt. Anlegen per printf (POSIX) - keine base64-
        Abhaengigkeit."""
        if self.askpass_path:
            return self.askpass_path
        content = "#!/bin/sh\nprintf '%s\\n' \"$__AP\"\n"
        suffix = f"{self.src['ip'].replace('.', '_')}_{secrets.token_hex(3)}"
        for base in ("/tmp", "/dev/shm"):
            path = f"{base}/.apm_{suffix}"
            cmd = (
                f"printf %s {shlex.quote(content)} > {shlex.quote(path)} "
                f"&& chmod 700 {shlex.quote(path)} "
                f"&& __AP=x {shlex.quote(path)} >/dev/null 2>&1 && echo EXEC_OK"
            )
            out, _err, _rc = self.run(cmd, 15)
            if "EXEC_OK" in out:
                self.askpass_path = path
                return path
        return None

    # -- Einzelner Test A -> B ------------------------------------------

    def test_target(self, tgt: dict) -> dict:
        host, port = tgt["ip"], tgt["port"]
        marker = "CONN_OK_" + secrets.token_hex(4).upper()
        started = time.monotonic()
        method, cmd = self._build_cmd(tgt, marker)
        out, err, rc = self.run(cmd, self.timeout + 5)
        latency_ms = int((time.monotonic() - started) * 1000)
        status, err_snippet = self._classify(out, err, rc, method, marker)
        return {
            "method": method,
            "status": status,
            "latency_ms": latency_ms,
            "error": err_snippet,
        }

    def _build_cmd(self, tgt: dict, marker: str) -> tuple[str, str]:
        host, port = tgt["ip"], tgt["port"]
        ssh_cmd = (
            f"ssh -p {port} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
            f"-o ConnectTimeout={self.timeout} -o PreferredAuthentications=password,keyboard-interactive "
            f"-o PubkeyAuthentication=no -o NumberOfPasswordPrompts=1 -o LogLevel=ERROR "
            f"{shlex.quote(f'{self.user}@{host}')} {shlex.quote('echo ' + marker)}"
        )
        tools = self.detect_tools()

        if tools.get("sshpass"):
            return "sshpass", f"sshpass -p {shlex.quote(self.password)} {ssh_cmd}"

        if tools.get("ssh"):
            script = self._ensure_askpass()
            if script:
                env = (
                    f"__AP={shlex.quote(self.password)} "
                    f"DISPLAY=:0 SSH_ASKPASS={shlex.quote(script)}"
                )
                if self.ssh_askpass_force:
                    env += " SSH_ASKPASS_REQUIRE=force"
                # setsid nur wenn vorhanden: paramiko-exec hat kein TTY, der
                # askpass-Trick funktioniert auch ohne.
                if tools.get("setsid"):
                    return "askpass", f"{env} setsid {ssh_cmd} < /dev/null 2>&1"
                return "askpass", f"{env} {ssh_cmd} < /dev/null 2>&1"

        if tools.get("nc"):
            return "port_nc", f"nc -z -w {self.timeout} {host} {port}; echo RC=$?"

        if tools.get("bash"):
            return (
                "port_bash",
                f"timeout {self.timeout} bash -c 'exec 3<>/dev/tcp/{host}/{port}' 2>&1; echo RC=$?",
            )

        return "no_tool", "true"

    def _classify(self, out: str, err: str, rc: int, method: str, marker: str):
        text = (out + "\n" + err).strip()
        low = text.lower()

        def snip() -> str:
            return text[:300]

        if method in ("sshpass", "askpass"):
            if marker in out:
                return "auth_ok", ""
            if ("permission denied" in low or "authentication failed" in low
                    or "no supported authentication methods" in low):
                return "auth_fail", snip()
            if "connection refused" in low:
                return "port_closed", snip()
            if ("timed out" in low or "no route to host" in low
                    or "network is unreachable" in low or "connection reset by peer" in low):
                return "net_unreachable", snip()
            return "tool_error", snip()

        if method == "port_nc":
            if rc == 0:
                return "port_open", ""
            if "timed out" in low or "unreachable" in low:
                return "net_unreachable", snip()
            if "refused" in low:
                return "port_closed", snip()
            return "port_closed", snip()

        if method == "port_bash":
            if rc == 0:
                return "port_open", ""
            if rc in (124, -1):
                return "net_unreachable", snip()
            if "refused" in low:
                return "port_closed", snip()
            if "unreachable" in low or "timed out" in low:
                return "net_unreachable", snip()
            return "port_closed", snip()

        return "no_tool", ""


# ---------------------------------------------------------------- Ausgabe

def make_row(src: dict, tgt: dict, method: str, status: str,
             latency_ms: int, error: str) -> list:
    return [
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        src["ip"], src["port"], src["label"], src["n24"], src["n16"], src["net"],
        tgt["ip"], tgt["port"], tgt["label"], tgt["n24"], tgt["n16"], tgt["net"],
        "forward", method, status, latency_ms, (error or "")[:300],
    ]


class PairBits:
    """Bitmap ueber alle geordneten Paare (src_id, tgt_id) - 1 Bit/Paar.
    Ersetzt die O(n^2)-Speicherstrukturen (pairs-Liste, done-Set): bei
    3557 Endpunkten (12,6 Mio. Paare) nur ~1,6 MB statt mehrerer GB."""

    def __init__(self, n: int):
        self.n = n
        self.bits = bytearray((n * n + 7) // 8)

    def set(self, src_id: int, tgt_id: int) -> None:
        idx = src_id * self.n + tgt_id
        self.bits[idx >> 3] |= 1 << (idx & 7)

    def get(self, src_id: int, tgt_id: int) -> bool:
        idx = src_id * self.n + tgt_id
        return bool(self.bits[idx >> 3] & (1 << (idx & 7)))


def build_in_scope(n: int, limit: int):
    """Bitmap der ersten `limit` geordneten Paare (a-, dann b-Reihenfolge).
    None = alle Paare im Scope (kein --limit-pairs)."""
    if limit <= 0 or limit >= n * (n - 1):
        return None
    bits = PairBits(n)
    count = 0
    for a in range(n):
        for b in range(n):
            if a == b:
                continue
            if count >= limit:
                return bits
            bits.set(a, b)
            count += 1
    return bits


def stream_detail(detail_path: str, id_of: dict, n: int, in_scope, done_bits,
                  retry_statuses, retry_bits, quota: int, quota_statuses: set,
                  direction_working, detail_counts, log) -> int:
    """Ein Streaming-Pass ueber detail.csv (keine Zeilenliste im RAM):
    - done_bits        : getestete Paare markieren (dedupliziert)
    - initial (Return) : Anzahl in-scope bereits getesteter Paare (dedupliziert)
    - retry_bits       : Paare mit Status in retry_statuses (falls gesetzt)
    - direction_working: Quell-IPs je Richtung (auf quota gekappt)
    - detail_counts    : Status-Verteilung ueber die ganze detail.csv
                         (fuer den Zwischenbericht: Vorlauf-Daten)
    """
    initial = 0
    if not os.path.exists(detail_path):
        return 0
    started = time.monotonic()
    log.info("detail.csv laden (%s)...", detail_path)
    with open(detail_path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                s = id_of.get((row["source_ip"], int(row["source_port"])))
                t = id_of.get((row["target_ip"], int(row["target_port"])))
            except (KeyError, ValueError):
                continue
            if s is None or t is None or s == t:
                continue
            st = row["status"]
            detail_counts[st] += 1
            if not done_bits.get(s, t):
                done_bits.set(s, t)
                if in_scope is None or in_scope.get(s, t):
                    initial += 1
            if retry_bits is not None and st in retry_statuses:
                retry_bits.set(s, t)
            if quota > 0 and st in quota_statuses:
                s_net = row.get("src_net") or row.get("src_24", "")
                t_net = row.get("tgt_net") or row.get("tgt_24", "")
                if s_net and t_net:
                    dw = direction_working[(s_net, t_net)]
                    if len(dw) < quota:  # nur die Laenge zaehlt -> kappen
                        dw.add(row["source_ip"])
    log.info("detail.csv geladen in %ds (%d getestete Paare gefunden)",
             int(time.monotonic() - started), initial)
    return initial


def prune_detail(detail_path: str, retry_statuses: set, id_of: dict, n: int,
                 log) -> int:
    """Entfernt Retry-Paare atomar aus detail.csv (streaming, kein RAM).
    Return: Anzahl entfernter Paare. Paare von IPs, die nicht mehr in der
    aktuellen Liste stehen, bleiben unangetastet."""
    if not os.path.exists(detail_path):
        log.error("--retry-status/--retry-all-failed braucht eine vorhandene "
                  "detail.csv (%s)", detail_path)
        sys.exit(2)

    retry_bits = PairBits(n)
    with open(detail_path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                s = id_of.get((row["source_ip"], int(row["source_port"])))
                t = id_of.get((row["target_ip"], int(row["target_port"])))
            except (KeyError, ValueError):
                continue
            if s is not None and t is not None and s != t \
                    and row["status"] in retry_statuses:
                retry_bits.set(s, t)

    pruned = 0
    tmp = detail_path + ".tmp"
    with open(detail_path, newline="", encoding="utf-8") as fh, \
            open(tmp, "w", newline="", encoding="utf-8") as fh2:
        w = csv.DictWriter(fh2, fieldnames=CSV_HEADER, restval="")
        w.writeheader()
        for row in csv.DictReader(fh):
            try:
                s = id_of.get((row["source_ip"], int(row["source_port"])))
                t = id_of.get((row["target_ip"], int(row["target_port"])))
            except (KeyError, ValueError):
                s = t = None
            if s is not None and t is not None and s != t and retry_bits.get(s, t):
                pruned += 1
                continue
            w.writerow(row)
    os.replace(tmp, detail_path)
    return pruned


class CsvWriter:
    """csv.writer + Zugriff auf die darunterliegende Datei (fuer flush)."""

    def __init__(self, fh):
        self._fh = fh
        self.writer = csv.writer(fh)

    def writerow(self, row) -> None:
        self.writer.writerow(row)

    def flush(self) -> None:
        self._fh.flush()


class RunStats:
    """Thread-sichere Lauf-Statistik (ersetzt die tqdm-basierte Progress-Bar).

    - total    = Umfang des aktuellen Laufs (inkl. bereits fertiger Paare)
    - initial  = bereits fertige Paare vor diesem Lauf (Resume-Offset)
    - real     = echte SSH-Tests dieses Laufs
    - instant  = sofort abgeschlossene Paare (z.B. source_unreachable)
    - counts   = Live-Zaehler je Status (dieser Lauf)
    - detail_counts = Status-Verteilung aus detail.csv (Vorlauf)
    - pps/real_s/threads_h = Historien fuer Graphen (TUI, btop-Look)
    """

    def __init__(self, total: int, initial: int = 0, history_len: int = 120):
        self.total = total
        self.initial = initial
        self.real = 0
        self.instant = 0
        self.counts = Counter()
        self.detail_counts = Counter()
        self.lock = threading.Lock()
        self.start = time.monotonic()
        self._last_sample = self.start
        self._last_done = self.initial
        self._last_real = 0
        self._last_status = 0.0
        self.pps = deque(maxlen=history_len)
        self.real_s = deque(maxlen=history_len)
        self.threads_h = deque(maxlen=history_len)
        # EMA-geglaettete Serien (sichtbare Kurven statt Spike+Null)
        self.pps_ema = deque(maxlen=history_len)
        self.real_ema = deque(maxlen=history_len)

    def update(self, n: int = 1, instant: bool = False, status: str = None) -> None:
        with self.lock:
            if instant:
                self.instant += n
            else:
                self.real += n
            if status:
                self.counts[status] += n

    def done(self) -> int:
        with self.lock:
            return self.initial + self.real + self.instant

    def remaining(self) -> int:
        with self.lock:
            return self.total - self.initial - self.real - self.instant

    def sample(self, active_threads: int) -> None:
        """Einen Messpunkt fuer die Graphen aufnehmen (TUI-Refresher und
        CLI-Einzeiler rufen das periodisch auf). Neben den rohen Raten
        werden EMA-geglaettete Serien (pps_ema/real_ema) gefuehrt, damit
        die Sparklines sichtbare Kurven zeigen statt Spike+Null."""
        with self.lock:
            now = time.monotonic()
            dt = now - self._last_sample
            if dt <= 0:
                return
            done_now = self.initial + self.real + self.instant
            pps = (done_now - self._last_done) / dt
            real_ps = (self.real - self._last_real) / dt
            self.pps.append(pps)
            self.real_s.append(real_ps)
            self.threads_h.append(active_threads)
            self.pps_ema.append(self._ema(self.pps_ema, pps))
            self.real_ema.append(self._ema(self.real_ema, real_ps))
            self._last_sample, self._last_done, self._last_real = \
                now, done_now, self.real

    # EMA-Glaettung fuer die Graphen-Serien.
    _EMA_ALPHA = 0.3

    @staticmethod
    def _ema(series: deque, value: float) -> float:
        if not series:
            return value
        a = RunStats._EMA_ALPHA
        return a * value + (1 - a) * series[-1]

    def _status_line_locked(self) -> str:
        """Status-Einzeiler bauen (Lock muss bereits gehalten sein)."""
        elapsed = time.monotonic() - self.start
        done = self.initial + self.real + self.instant
        pct = 100.0 * done / self.total if self.total else 0.0
        rate = self.real / elapsed if elapsed > 0 and self.real else 0.0
        remaining = self.total - done
        eta = fmt_duration(remaining / rate) if rate > 0 and remaining > 0 else "unbekannt"
        parts = [f"{fmt_num(done)}/{fmt_num(self.total)} ({pct:.2f}%)",
                 f"{rate:.1f}/s", f"ETA {eta}"]
        if remaining > 0:
            parts.append(f"noch {fmt_num(remaining)}")
        for st in STATUS_ORDER:
            n = self.counts.get(st, 0)
            if n:
                parts.append(f"{STATUS_SHORT[st]}:{fmt_num(n)}")
        return " · ".join(parts)

    def status_line(self) -> str:
        """Kompakter Status-Einzeiler (CLI-Modus)."""
        with self.lock:
            return self._status_line_locked()

    def maybe_print_status(self, interval: int) -> None:
        """CLI: periodischer Status-Einzeiler (alle `interval` Sekunden)."""
        if interval <= 0:
            return
        now = time.monotonic()
        with self.lock:
            if now - self._last_status < interval:
                return
            self._last_status = now
            line = self._status_line_locked()
        print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {line}",
              file=sys.stderr)

    def snapshot(self) -> dict:
        """Atomarer Daten-Schnappschuss fuer die TUI."""
        with self.lock:
            return {
                "total": self.total,
                "initial": self.initial,
                "real": self.real,
                "instant": self.instant,
                "counts": dict(self.counts),
                "detail_counts": dict(self.detail_counts),
                "pps": list(self.pps),
                "real_s": list(self.real_s),
                "pps_ema": list(self.pps_ema),
                "real_ema": list(self.real_ema),
                "threads_h": list(self.threads_h),
                "start": self.start,
                "done": self.initial + self.real + self.instant,
                "remaining": self.total - self.initial - self.real - self.instant,
            }


def id_chunks(n: int, k: int):
    """(start, end)-Bereiche ueber die Host-Indizes 0..n-1 in k Chunks."""
    if k <= 1:
        yield (0, n)
        return
    chunk = max(1, -(-n // k))
    for start in range(0, n, chunk):
        yield (start, min(start + chunk, n))


class RunContext:
    """Geteilte Lauf-Strukturen (Hosts + Bitmaps) fuer Worker/Consumer/Menue."""

    def __init__(self, hosts: list, scope_bits, done_bits: PairBits):
        self.hosts = hosts
        self.scope_bits = scope_bits
        self.done_bits = done_bits


def iter_targets(ctx, src_id: int, id_range: tuple) -> iter:
    """Alle Ziele einer Quelle im id_range, gefiltert nach Scope/Resume."""
    hosts, scope_bits, done_bits = ctx.hosts, ctx.scope_bits, ctx.done_bits
    for tgt_id in range(id_range[0], id_range[1]):
        if tgt_id == src_id:
            continue
        if scope_bits is not None and not scope_bits.get(src_id, tgt_id):
            continue
        if done_bits.get(src_id, tgt_id):
            continue
        yield hosts[tgt_id]


def skip_first(it: iter, k: int) -> iter:
    for i, item in enumerate(it):
        if i >= k:
            yield item


def worker(src_id, id_range, ctx, config, user, password, writer, lock,
           stats, direction_working, stop_event, log) -> tuple:
    src = ctx.hosts[src_id]

    def targets_iter():
        return iter_targets(ctx, src_id, id_range)

    tester = SourceTester(src, user, password, config)
    # Connect mit pause-and-retry-when-auth-failed
    connect_retries = 0
    while True:
        ok, err = tester.connect()
        if ok:
            break
        is_auth = "auth" in err.lower()
        if is_auth and config.auth_pause > 0 and connect_retries < config.auth_pause_retries:
            # globaler Block-Pause
            config.record_auth_fail(stop_event, log)
            connect_retries += 1
            if stop_event.is_set():
                return src_id, 0, 0
            log.info("Retry connect %s:%s (%d/%d) nach Pause",
                     src["ip"], src["port"], connect_retries, config.auth_pause_retries)
            continue
        elif is_auth and config.auth_pause > 0:
            config.record_auth_fail(stop_event, log)
        break
    if not ok:
        # bei globaler Pause ggf. mitwarten (andere Worker hat Block ausgeloest)
        if "auth" in err.lower() and config.auth_pause > 0:
            config.wait_if_paused(stop_event)
        with lock:
            n_skipped = 0
            n_srcerr = 0
            for tgt in targets_iter():
                if config.subnet_quota > 0:
                    direction = (src["net"], tgt["net"])
                    if len(direction_working[direction]) >= config.subnet_quota:
                        writer.writerow(make_row(src, tgt, "quota_skip", "skipped", 0, ""))
                        n_skipped += 1
                        continue
                writer.writerow(make_row(src, tgt, "connect", "source_unreachable", 0, err))
                n_srcerr += 1
            writer.flush()
            # Batch-Update: ein Sprung pro Status statt 219x +1
            if n_skipped:
                stats.update(n_skipped, instant=True, status="skipped")
            if n_srcerr:
                stats.update(n_srcerr, instant=True, status="source_unreachable")
        # status fuer Statistik: source_unreachable vs source_auth_fail unterscheiden?
        log.warning("Quelle %s:%s nicht erreichbar: %s", src["ip"], src["port"], err)
        return src_id, n_skipped + n_srcerr, 0

    tester.detect_tools()
    log.info("Quelle %s:%s verbunden, Tools: %s", src["ip"], src["port"],
             {k: v for k, v in tester.tools.items() if v})

    consec = 0
    processed = 0
    for tgt in targets_iter():
        # globaler Auth-Pause (andere Worker hat Block erkannt) abwarten
        config.wait_if_paused(stop_event)
        # Stop angefordert? Restliche Ziele ungeschrieben lassen -> Resume.
        if stop_event.is_set():
            break

        # Subnetz-Quota: Richtung schon ausreichend getestet?
        if config.subnet_quota > 0:
            direction = (src["net"], tgt["net"])
            with lock:
                if len(direction_working[direction]) >= config.subnet_quota:
                    writer.writerow(make_row(src, tgt, "quota_skip", "skipped", 0, ""))
                    writer.flush()
                    stats.update(1, instant=True, status="skipped")
                    processed += 1
                    continue

        # A->B mit pause-and-retry-when-auth-failed
        tgt_retries = 0
        while True:
            res = tester.test_target(tgt)
            if res["status"] == "auth_fail" and config.auth_pause > 0:
                # fuer Block-Erkennung zaehlen + ggf. globale Pause
                is_trigger = config.record_auth_fail(stop_event, log)
                if tgt_retries < config.auth_pause_retries:
                    tgt_retries += 1
                    log.info("Retry auth %s->%s (%d/%d) nach Pause %ds",
                             src["ip"], tgt["ip"], tgt_retries,
                             config.auth_pause_retries, config.auth_pause)
                    if stop_event.is_set():
                        break
                    continue  # gleiches Ziel erneut
                # kein Retry mehr - finaler auth_fail wird unten geschrieben
                break
            break
        with lock:
            writer.writerow(make_row(src, tgt, res["method"], res["status"],
                                     res["latency_ms"], res["error"]))
            writer.flush()
            stats.update(1, status=res["status"])
            if config.subnet_quota > 0 and res["status"] in config.quota_statuses:
                dw = direction_working[(src["net"], tgt["net"])]
                if len(dw) < config.subnet_quota:  # nur Laenge zaehlt -> kappen
                    dw.add(src["ip"])
        if (res["status"] == "tool_error"
                and (not res["error"]
                     or "open_session" in res["error"]
                     or "exec_command timeout" in res["error"])):
            consec += 1
        else:
            consec = 0
        if consec >= 3:
            log.warning("Quelle %s:%s: Verbindung scheint tot, Reconnect", src["ip"], src["port"])
            ok, err = tester.reconnect()
            if not ok:
                with lock:
                    n_rest = 0
                    for r in skip_first(targets_iter(), processed + 1):
                        writer.writerow(make_row(src, r, "connect",
                                                 "source_unreachable", 0, err))
                        n_rest += 1
                    writer.flush()
                    if n_rest:
                        stats.update(n_rest, instant=True, status="source_unreachable")
                log.warning("Reconnect fehlgeschlagen, %d Ziele als "
                            "source_unreachable markiert", n_rest)
                break
            consec = 0
        processed += 1

    tester.cleanup()
    return src_id, processed, 0


# ---------------------------------------------------------------- Worker-Pool

def consumer(work_queue, config, stop_event, user, password, writer, lock,
             stats, direction_working, log, ctx):
    """Ein Consumer-Thread: zieht Tasks aus der Queue und fuehrt sie aus.
    Beendet sich bei Stop, bei leerer Queue oder wenn ueberzaehlig
    (Worker-Anpassung im Pause-Menue)."""
    while not stop_event.is_set():
        # Shrink: ueberschuessige Consumer beenden sich nach aktuellem Task.
        with config.lock:
            if config.active_workers > config.target_workers:
                config.active_workers -= 1
                return
        try:
            src_id, id_range = work_queue.get(timeout=0.5)
        except queue.Empty:
            with config.lock:
                config.active_workers -= 1
            return
        try:
            worker(src_id, id_range, ctx, config, user, password, writer,
                   lock, stats, direction_working, stop_event, log)
        except Exception as exc:
            log.exception("Worker-Fehler fuer Quelle %s", ctx.hosts[src_id]["ip"])
        finally:
            work_queue.task_done()
    with config.lock:
        config.active_workers -= 1


def spawn_consumers(n, work_queue, config, stop_event, user, password, writer,
                    lock, stats, direction_working, log, ctx):
    """n neue Consumer-Threads starten."""
    for _ in range(n):
        with config.lock:
            config.active_workers += 1
        threading.Thread(
            target=consumer,
            args=(work_queue, config, stop_event, user, password, writer,
                  lock, stats, direction_working, log, ctx),
            daemon=True,
        ).start()


def set_workers(config, n, work_queue, stop_event, user, password, writer,
                lock, stats, direction_working, log, ctx) -> None:
    """Worker-Anzahl zur Laufzeit aendern. Mehr -> spawnen, weniger ->
    ueberschuessige Consumer beenden sich nach ihrem aktuellen Task."""
    if n < 0:
        n = 0
    with config.lock:
        config.target_workers = n
        current = config.active_workers
    if n > current:
        spawn_consumers(n - current, work_queue, config, stop_event, user,
                        password, writer, lock, stats, direction_working,
                        log, ctx)


# ---------------------------------------------------------------- Zwischenbericht

def fmt_num(n: int) -> str:
    """Zahl mit Punkt-Tausendertrennung (z.B. 12648692 -> 12.648.692)."""
    return f"{n:,}".replace(",", ".")


def interim_report_lines(progress: RunStats, direction_working, config) -> list:
    """Sprechender Zwischenbericht (Zeilenliste) fuer die TUI.
    Kumulativ: Vorlauf-Daten aus detail.csv (progress.detail_counts)
    plus Zaehler des aktuellen Laufs. Farben als Textual-Markup
    ([b], [cyan], ...) - die TUI rendert das im RichLog.

    WICHTIG: Hier bewusst KEIN ANSI (paint/colored), sonst zeigt das
    ReportModal Escape-Sequenzen statt Farben."""

    def mk(tag: str, text: str, bold: bool = False) -> str:
        if bold:
            return f"[b][{tag}]{text}[/{tag}][/b]"
        return f"[{tag}]{text}[/{tag}]"

    counts = progress.counts
    detail_counts = getattr(progress, "detail_counts", None) or Counter()
    done = progress.initial + progress.real + progress.instant
    pct = 100.0 * done / progress.total if progress.total else 0.0
    elapsed = time.monotonic() - progress.start
    rate = progress.real / elapsed if elapsed > 0 and progress.real else 0.0
    remaining = progress.total - done
    eta = fmt_duration(remaining / rate) if rate > 0 and remaining > 0 else "unbekannt"

    out = [""]
    out.append(mk("cyan", "=========== ZWISCHENBERICHT ===========", bold=True))
    out.append(f"Fortschritt: [b]{fmt_num(done)}[/b] von "
               f"{fmt_num(progress.total)} Paaren ({pct:.2f}%)")
    if progress.initial:
        out.append(f"  davon {fmt_num(progress.initial)} bereits getestet (Vorlauf), "
                   f"{fmt_num(progress.real + progress.instant)} in diesem Lauf")
    out.append(f"  {fmt_num(progress.real)} echte SSH-Tests, "
               f"{fmt_num(progress.instant)} sofort markiert · "
               f"{rate:.1f} Tests/Sek · Restdauer ca. {eta}")
    out.append("")

    merged = Counter(detail_counts)
    merged.update(counts)
    out.append(mk("cyan", "Status gesamt (Vorlauf + Lauf):"))
    any_count = False
    for st in STATUS_ORDER:
        n = merged.get(st, 0)
        if n:
            any_count = True
            color = STATUS_MARKUP.get(st, "")
            line = f"  {fmt_num(n):>12}  {STATUS_DESCRIPTIONS[st]}"
            out.append(mk(color, line) if color else line)
    if not any_count:
        out.append("  (noch keine Testergebnisse vorhanden)")
    this_run = {st: n for st, n in counts.items() if n}
    if this_run:
        kurz = ", ".join(f"{STATUS_SHORT[st]}:{fmt_num(n)}" for st, n in this_run.items())
        out.append(f"  [dim](davon in diesem Lauf: {kurz})[/dim]")
    out.append("")

    if config.subnet_quota > 0:
        confirmed = sum(1 for v in direction_working.values()
                        if len(v) >= config.subnet_quota)
        out.append(f"[cyan]Subnetz-Erreichbarkeit:[/] {confirmed} von "
                   f"{len(direction_working)} Richtungen bestaetigt "
                   f"(Quota {config.subnet_quota}, Modus {config.quota_mode})")
        problems = sorted(
            ((s_net, t_net, len(srcs)) for (s_net, t_net), srcs
             in direction_working.items() if len(srcs) < config.subnet_quota),
            key=lambda x: x[2], reverse=True)
        if problems:
            for s_net, t_net, n in problems[:10]:
                out.append(f"  [yellow]{s_net} -> {t_net}:[/] "
                           f"{n} Quell-Hosts bestaetigt (Quota {config.subnet_quota} "
                           f"noch nicht erreicht)")
            if len(problems) > 10:
                out.append(f"  ... und {len(problems) - 10} weitere Richtungen "
                           f"mit Luecken")
    else:
        out.append("(Keine Subnetz-Quota aktiv - alle Richtungen werden voll getestet)")
    out.append(mk("cyan", "=======================================", bold=True))
    out.append("")
    return out


# ---------------------------------------------------------------- Main

def parse_args():
    ap = argparse.ArgumentParser(
        description="SSH-Matrix-Tester: prueft fuer alle IP-Paare, ob Quelle A "
                    "per SSH-Login Ziel B erreichen kann (beide Richtungen).")
    ap.add_argument("--version", action="version",
                    version=f"%(prog)s {VERSION} (entwickelt von {AUTHOR})")
    ap.add_argument("--verbose", choices=["err", "warn", "info"], default="info",
                    help="Detailgrad der Terminal-Ausgabe (stderr): err = nur "
                         "Fehler, warn = Fehler + Warnungen, info = alles "
                         "(Default). run.log bleibt immer vollstaendig.")
    ap.add_argument("--force", action="store_true",
                    help="RAM-Warnung (Schaetzung ueber Schwelle, Default 1 GB) "
                         "ohne Nachfrage ueberschreiben")
    ap.add_argument("--ips", required=True,
                    help="Pfad zur IP-Liste (Format siehe README / ips.txt.example)")
    ap.add_argument("--user", required=True, help="SSH-User (gilt fuer alle IPs)")
    pw = ap.add_mutually_exclusive_group(required=True)
    pw.add_argument("--pass-env", default=None,
                    help="Name der Umgebungsvariable mit dem Passwort (Default: SSHPASS)")
    pw.add_argument("--pass-file", default=None,
                    help="Datei, deren erste Zeile das Passwort ist")
    ap.add_argument("--port-default", type=int, default=DEFAULT_PORT,
                    help="Default-SSH-Port fuer IPs ohne :PORT (Default: 22)")
    ap.add_argument("--workers", type=int, default=20,
                    help="Parallele Quell-IPs (Default: 20)")
    ap.add_argument("--timeout", type=int, default=10,
                    help="Connect-/Befehls-Timeout in Sekunden je Hop (Default: 10)")
    ap.add_argument("--per-source", type=int, default=1,
                    help="Max. gleichzeitige Tests pro Quell-IP (Default: 1)")
    ap.add_argument("--out", default="ssh_matrix_out",
                    help="Ausgabe-Verzeichnis (Default: ssh_matrix_out)")
    ap.add_argument("--resume", action="store_true",
                    help="Bereits getestete Paare in detail.csv ueberspringen")
    ap.add_argument("--retry-status", default=None,
                    help="Komma-getrennte Status, die neu getestet werden sollen "
                         "(impliziert --resume). Z.B. "
                         "source_unreachable,net_unreachable,tool_error. "
                         "Gueltig: " + ",".join(sorted(KNOWN_STATUSES)))
    ap.add_argument("--retry-all-failed", action="store_true",
                    help="Shortcut: alle Paare ausser auth_ok/skipped neu testen "
                         "(impliziert --resume)")
    ap.add_argument("--subnet-quota", type=int, default=0,
                    help="Mindestanzahl erfolgreicher Quell-Hosts pro Richtung "
                         "(src_net -> tgt_net); danach Rest als 'skipped' "
                         "ueberspringen. 0 = aus (Default: 0)")
    ap.add_argument("--quota-mode", choices=sorted(QUOTA_MODES), default="auth_ok",
                    help="Was zaehlt als 'funktionierender Quell-Host' fuer "
                         "--subnet-quota: auth_ok (nur voller Login, Default) "
                         "oder reachable (netzwerkseitig erreichbar: auth_ok, "
                         "auth_fail oder port_open)")
    ap.add_argument("--subnet-gap", type=int, default=16,
                    help="Luecken-Schwellwert fuer Subnetz-Clustering (Default: 16). "
                         "0 = feste /24 wie bisher")
    ap.add_argument("--limit-pairs", type=int, default=0,
                    help="Nur die ersten N Paare testen (Dry-Run/Trockentest)")
    ap.add_argument("--tui", action="store_true",
                    help="TUI erzwingen (Textual; braucht: pip install textual). "
                         "Ohne Flag: Auto-Detect (TTY + textual -> TUI)")
    ap.add_argument("--no-tui", action="store_true",
                    help="TUI deaktivieren (CLI-Modus erzwingen)")
    ap.add_argument("--status-interval", type=int, default=30,
                    help="CLI: periodischer Status-Einzeiler alle N Sekunden "
                         "(Default: 30, 0 = aus)")
    ap.add_argument("--auth-pause", type=parse_duration, default=0,
                    help="Bei Auth-Fail Block: pausiere DURATION (z.B. 5m, 300s, 2h) "
                         "und retry. 0=aus (Default). Beispiel: --auth-pause 5m")
    ap.add_argument("--auth-pause-threshold", type=int, default=3,
                    help="Auth-Fails im Fenster bis Pause triggert (Default: 3)")
    ap.add_argument("--auth-pause-window", type=parse_duration, default=60,
                    help="Fenster fuer Auth-Fails (Default: 60s, z.B. 60, 2m)")
    ap.add_argument("--auth-pause-retries", type=int, default=1,
                    help="Retries pro Paar nach Pause (Default: 1)")
    return ap.parse_args()


def resolve_password(args) -> str:
    if args.pass_file:
        with open(args.pass_file, "r", encoding="utf-8-sig") as fh:
            pw = fh.readline().rstrip("\r\n")
            # BOM (\ufeff) wird durch utf-8-sig bereits entfernt
            if pw != pw.strip() and pw.strip():
                print(f"WARNUNG: Passwort aus {args.pass_file} hat fuehrende/anhängende "
                      f"Leerzeichen/Tabs — wird unverändert verwendet "
                      f"(len={len(pw)} vs stripped={len(pw.strip())})",
                      file=sys.stderr)
            # Leere nach strip testen, BOM bereits weg
            if not pw:
                print(f"FEHLER: --pass-file {args.pass_file} ist leer", file=sys.stderr)
                sys.exit(2)
            if pw.startswith("\ufeff"):
                pw = pw.lstrip("\ufeff")
            return pw
    env = args.pass_env or "SSHPASS"
    pw = os.environ.get(env)
    if pw is None:
        print(f"FEHLER: Umgebungsvariable {env} ist nicht gesetzt "
              f"(export {env}='...')", file=sys.stderr)
        sys.exit(2)
    if pw == "":
        print(f"FEHLER: Umgebungsvariable {env} ist leer", file=sys.stderr)
        sys.exit(2)
    if pw != pw.strip() and pw.strip():
        print(f"WARNUNG: Passwort aus ${env} hat fuehrende/anhängende Leerzeichen "
              f"(len={len(pw)} vs stripped={len(pw.strip())})", file=sys.stderr)
    if pw.startswith("\ufeff"):
        pw = pw.lstrip("\ufeff")
        print(f"WARNUNG: Passwort aus ${env} hatte BOM — entfernt", file=sys.stderr)
    return pw


def main():
    args = parse_args()
    password = resolve_password(args)

    os.makedirs(args.out, exist_ok=True)
    log = logging.getLogger("ssh_matrix")
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.FileHandler(os.path.join(args.out, "run.log"), encoding="utf-8")
    fh.setFormatter(fmt)
    fh.setLevel(logging.INFO)  # run.log immer vollstaendig (Diagnose)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(ColorFormatter("%(asctime)s %(levelname)s %(message)s"))
    # --verbose steuert NUR die stderr-Ausgabe (nicht run.log)
    sh.setLevel(LOG_LEVELS[args.verbose])
    log.addHandler(fh)
    log.addHandler(sh)

    # TUI-Aktivierung: --tui erzwingt, --no-tui deaktiviert, sonst Auto-Detect
    # (stderr+stdin TTY UND textual installiert).
    use_tui = False
    tui_log_handler = None
    if args.tui or (not args.no_tui and sys.stderr.isatty() and sys.stdin.isatty()):
        try:
            from ssh_matrix_tui import TuiLogHandler, run_tui
        except ImportError:
            if args.tui:
                print("FEHLER: textual ist nicht installiert. Installation: "
                      "pip3 install --break-system-packages textual",
                      file=sys.stderr)
                sys.exit(2)
            run_tui = None
        else:
            use_tui = True
            log.removeHandler(sh)  # stderr gehoert jetzt der TUI
            tui_log_handler = TuiLogHandler()
            tui_log_handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            log.addHandler(tui_log_handler)

    if not use_tui:
        print_banner()
    log.info("SSH-Matrix-Tester %s - entwickelt von %s (verbose=%s, tui=%s)",
             VERSION, AUTHOR, args.verbose, use_tui)

    hosts = parse_ips_file(args.ips, args.port_default, log)
    if not hosts:
        log.error("Keine gueltigen IPs in %s gefunden", args.ips)
        sys.exit(1)
    cluster_subnets(hosts, args.subnet_gap)
    nets = sorted(set(h["net"] for h in hosts))
    log.info("%d Endpunkte geladen, %d Subnetze (gap=%d)", len(hosts), len(nets),
             args.subnet_gap)
    if args.subnet_quota > 0:
        log.info("--subnet-quota %d (Modus %s): pro Richtung werden nach %d "
                 "Quell-Hosts (%s) die restlichen Paare als 'skipped' "
                 "uebersprungen",
                 args.subnet_quota, args.quota_mode, args.subnet_quota,
                 "/".join(sorted(QUOTA_MODES[args.quota_mode])))

    n = len(hosts)
    id_of = {(h["ip"], h["port"]): i for i, h in enumerate(hosts)}
    all_pairs = n * (n - 1)
    log.info("%d geordnete Paare (beide Richtungen)", all_pairs)

    # Retry-Flags auswerten (implizieren Resume).
    retry_statuses = None
    if args.retry_all_failed:
        retry_statuses = KNOWN_STATUSES - RETRY_ALL_EXCLUDE
    if args.retry_status:
        requested = {s.strip() for s in args.retry_status.split(",") if s.strip()}
        unknown = requested - KNOWN_STATUSES
        if unknown:
            log.error("Unbekannte Status in --retry-status: %s. Gueltig: %s",
                      ",".join(sorted(unknown)), ",".join(sorted(KNOWN_STATUSES)))
            sys.exit(2)
        retry_statuses = (retry_statuses or set()) | requested

    # RAM-Warnung vor dem Start (Schaetzung, Streaming-Architektur).
    with_resume = bool(args.resume or retry_statuses)
    est = estimate_ram_mb(n)
    if est > RAM_WARN_MB:
        log.warning("Geschaetzter RAM-Bedarf ~%d MB fuer %d Endpunkte (%d Paare, "
                    "resume=%s) - Schwelle %d MB ueberschritten.",
                    int(est), n, all_pairs, with_resume, RAM_WARN_MB)
        if not args.force:
            if not sys.stdin.isatty():
                log.error("Kein Terminal fuer Bestaetigung - Abbruch "
                          "(oder --force verwenden).")
                sys.exit(0)
            answer = input("Trotzdem fortfahren? [j/N]: ").strip().lower()
            if answer not in ("j", "ja", "y", "yes"):
                log.info("Abgebrochen - Speicherwarnung.")
                sys.exit(0)

    detail_path = os.path.join(args.out, "detail.csv")

    total = all_pairs
    if args.limit_pairs and 0 < args.limit_pairs < total:
        total = args.limit_pairs
        log.info("--limit-pairs: teste nur %d Paare", total)
    scope_bits = build_in_scope(n, args.limit_pairs)

    direction_working = defaultdict(set)  # (src_net, tgt_net) -> Quell-IPs (auf quota gekappt)

    if retry_statuses:
        pruned = prune_detail(detail_path, retry_statuses, id_of, n, log)
        log.info("--retry-status: %d Paare mit Status %s zum Re-Test markiert",
                 pruned, ",".join(sorted(retry_statuses)))

    done_bits = PairBits(n)
    detail_counts = Counter()
    initial = stream_detail(detail_path, id_of, n, scope_bits, done_bits,
                            retry_statuses, None,
                            args.subnet_quota, QUOTA_MODES[args.quota_mode],
                            direction_working, detail_counts, log)
    remaining = total - initial
    if retry_statuses:
        log.info("--retry-status: %d Paare verbleiben insgesamt (davon %d Re-Test)",
                 remaining, pruned)
    elif args.resume:
        log.info("--resume: %d Paare bereits getestet, %d verbleiben",
                 initial, remaining)
    if remaining <= 0:
        log.info("Keine Paare mehr zu testen. Report: "
                 "python3 ssh_matrix_report.py --detail %s --out %s", detail_path, args.out)
        sys.exit(0)

    if args.subnet_quota > 0 and os.path.exists(detail_path):
        met = sum(1 for v in direction_working.values() if len(v) >= args.subnet_quota)
        log.info("--subnet-quota: %d Richtungen aus detail.csv geladen (%d bereits erfuellt)",
                 len(direction_working), met)

    tasks = []
    for src_id in range(n):
        for (start, end) in id_chunks(n, args.per_source):
            tasks.append((src_id, (start, end)))

    write_mode = "a" if (os.path.exists(detail_path) and os.path.getsize(detail_path) > 0) else "w"
    csvf = open(detail_path, write_mode, newline="", encoding="utf-8")
    writer = CsvWriter(csvf)
    if write_mode == "w":
        writer.writerow(CSV_HEADER)
        writer.flush()
    lock = threading.Lock()
    stats = RunStats(total=total, initial=initial)
    stats.detail_counts = detail_counts  # fuer den Zwischenbericht (Vorlauf)
    ctx = RunContext(hosts, scope_bits, done_bits)

    log.info("Start: %d Aufgaben, %d Worker, timeout=%ds, per-source=%d",
             len(tasks), args.workers, args.timeout, args.per_source)

    config = RunConfig(args)
    config.stream_handler = sh if not use_tui else None
    work_queue = queue.Queue()
    for task in tasks:
        work_queue.put(task)
    stop_event = threading.Event()

    spawn_consumers(args.workers, work_queue, config, stop_event, args.user,
                    password, writer, lock, stats, direction_working, log, ctx)

    started = time.monotonic()
    if use_tui:
        # TUI: blockiert bis Stop/Quit (bestätigt per Modal).
        run_tui(config, stats, direction_working, work_queue, stop_event,
                args.user, password, writer, lock, log, ctx, tui_log_handler)
        log.warning("TUI beendet - sauberer Stop, Rest bleibt fuer --resume erhalten")
    else:
        while not stop_event.is_set():
            with config.lock:
                active = config.active_workers
            if work_queue.empty() and active == 0:
                break  # alles getestet, alle Consumer fertig
            stats.maybe_print_status(args.status_interval)
            stats.sample(active)
            try:
                time.sleep(0.5)
            except KeyboardInterrupt:
                # 1x Ctrl+C = immer sauberer Stop (resume-faehig).
                # 2x Ctrl+C (waehrend des Shutdown) = hart, siehe unten.
                log.warning("Stop angefordert (Ctrl+C) - Worker beenden "
                            "aktuellen Test, Rest bleibt fuer --resume erhalten")
                break

    # Sauber herunterfahren: warten bis alle Consumer ihren aktuellen Task
    # beendet haben (begrenzt, damit nichts haengt).
    stop_event.set()
    wait_deadline = time.monotonic() + 120
    try:
        while time.monotonic() < wait_deadline:
            with config.lock:
                active = config.active_workers
            if active == 0:
                break
            time.sleep(0.2)
    except KeyboardInterrupt:
        log.warning("Abbruch waehrend Shutdown - hart beendet")
        try:
            csvf.flush()
        except Exception:
            pass
        os._exit(130)

    stats.sample(0)
    csvf.close()
    elapsed = time.monotonic() - started

    log.info("Fertig in %ds. Auswertung: python3 ssh_matrix_report.py --detail %s --out %s",
             int(elapsed), detail_path, args.out)
    ok_count = stats.counts.get("auth_ok", 0)
    skip_count = stats.counts.get("skipped", 0)
    fail_count = stats.real + stats.instant - ok_count - skip_count
    if USE_COLOR and not use_tui:
        print(f"\n{C_GREEN}Fertig{C_RESET} in {int(elapsed)}s. "
              f"{colored('auth_ok', f'{ok_count} OK')}, "
              f"{colored('auth_fail', f'{fail_count} Fehler')}, "
              f"{colored('skipped', f'{skip_count} SKIP')}. "
              f"Ergebnisse: {detail_path}", file=sys.stderr)
        print(f"{C_CYAN}Report erzeugen:{C_RESET}", file=sys.stderr)
        print(f"  python3 ssh_matrix_report.py --detail {detail_path} --out {args.out}",
              file=sys.stderr)
    elif not use_tui:
        print(f"\nFertig in {int(elapsed)}s. {ok_count} OK, {fail_count} Fehler, "
              f"{skip_count} SKIP. Ergebnisse: {detail_path}", file=sys.stderr)
        print("Report erzeugen:", file=sys.stderr)
        print(f"  python3 ssh_matrix_report.py --detail {detail_path} --out {args.out}",
              file=sys.stderr)


if __name__ == "__main__":
    main()
