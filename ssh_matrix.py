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
import base64
import csv
import ipaddress
import logging
import os
import re
import secrets
import shlex
import socket
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

try:
    import paramiko
except ImportError:
    print("FEHLER: paramiko fehlt. Auf dem Kali-Host installieren:", file=sys.stderr)
    print("  sudo apt install -y python3-paramiko", file=sys.stderr)
    sys.exit(2)

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

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


def colored(status: str, text: str) -> str:
    if not USE_COLOR:
        return text
    col = STATUS_COLORS.get(status, "")
    return f"{col}{text}{C_RESET}" if col else text


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

    def __init__(self, src: dict, user: str, password: str, timeout: int):
        self.src = src
        self.user = user
        self.password = password
        self.timeout = timeout
        self.client = None
        self.tools = None
        self.ssh_askpass_force = False
        self.askpass_path = None

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
                self.client = client
                return True, ""
            except (socket.timeout, paramiko.AuthenticationException,
                    paramiko.SSHException, OSError) as exc:
                last = str(exc)
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
            chan = self.client.get_transport().open_session()
        except Exception as exc:
            return "", str(exc), -1
        chan.settimeout(1)
        try:
            chan.exec_command(cmd)
        except Exception as exc:
            return "", str(exc), -1
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
        while True:
            try:
                data = chan.recv(65536)
            except Exception:
                break
            if not data:
                break
            out.append(data.decode("utf-8", "replace"))
        while True:
            try:
                data = chan.recv_stderr(65536)
            except Exception:
                break
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
        out, err, _rc = self.run(probe, 15)
        text = out + "\n" + err
        self.tools = {name: (f"HAVE:{name}" in text) for name in TOOL_PROBE.split()}
        m = re.search(r"OpenSSH[_-](\d+)\.(\d+)", text)
        self.ssh_askpass_force = bool(m) and (int(m.group(1)), int(m.group(2))) >= (8, 4)
        return self.tools

    def _ensure_askpass(self) -> str | None:
        """Askpass-Skript auf A anlegen (liest Passwort aus Env __AP, enthaelt
        es NICHT selbst). /tmp bevorzugt, /dev/shm als Fallback. Wird in
        cleanup() entfernt."""
        if self.askpass_path:
            return self.askpass_path
        content = "#!/bin/sh\nprintf '%s\\n' \"$__AP\"\n"
        b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
        suffix = f"{self.src['ip'].replace('.', '_')}_{secrets.token_hex(3)}"
        for base in ("/tmp", "/dev/shm"):
            path = f"{base}/.apm_{suffix}"
            cmd = (
                f"echo {shlex.quote(b64)} | base64 -d > {shlex.quote(path)} "
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
            f"-o ConnectTimeout={self.timeout} -o PreferredAuthentications=password "
            f"-o PubkeyAuthentication=no -o NumberOfPasswordPrompts=1 -o LogLevel=ERROR "
            f"{shlex.quote(f'{self.user}@{host}')} {shlex.quote('echo ' + marker)}"
        )
        tools = self.detect_tools()

        if tools.get("sshpass"):
            return "sshpass", f"sshpass -p {shlex.quote(self.password)} {ssh_cmd}"

        if tools.get("ssh") and tools.get("setsid"):
            script = self._ensure_askpass()
            if script:
                env = (
                    f"__AP={shlex.quote(self.password)} "
                    f"DISPLAY=:0 SSH_ASKPASS={shlex.quote(script)}"
                )
                if self.ssh_askpass_force:
                    env += " SSH_ASKPASS_REQUIRE=force"
                return "askpass", f"{env} setsid {ssh_cmd} < /dev/null 2>&1"

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


def pair_key(pair) -> tuple:
    src, tgt = pair
    return (src["ip"], src["port"], tgt["ip"], tgt["port"])


def load_completed(detail_path: str) -> set:
    done = set()
    if not os.path.exists(detail_path):
        return done
    with open(detail_path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            done.add((row["source_ip"], int(row["source_port"]),
                      row["target_ip"], int(row["target_port"])))
    return done


def prune_detail(detail_path: str, retry_statuses: set, current_keys: set,
                 log) -> tuple[set, int]:
    """Entfernt Paare aus detail.csv, deren Last-Status in retry_statuses ist
    und die noch in der aktuellen IP-Liste (current_keys) vorkommen. Paare von
    IPs, die nicht mehr in der Liste stehen, bleiben unangetastet.

    Return: (done_set, anzahl_entfernter_Paare). detail.csv wird atomar neu
    geschrieben (Temp-Datei + Rename)."""
    if not os.path.exists(detail_path):
        log.error("--retry-status/--retry-all-failed braucht eine vorhandene "
                  "detail.csv (%s)", detail_path)
        sys.exit(2)

    with open(detail_path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        log.warning("detail.csv ist leer - nichts zu retryen")
        return set(), 0

    # Last-Status pro Paar (letzte Vorkommen gewinnt).
    last_status = {}
    for r in rows:
        key = (r["source_ip"], int(r["source_port"]),
               r["target_ip"], int(r["target_port"]))
        last_status[key] = r["status"]

    retry_pairs = {
        key for key, st in last_status.items()
        if st in retry_statuses and key in current_keys
    }

    keep = [r for r in rows
            if ((r["source_ip"], int(r["source_port"]),
                 r["target_ip"], int(r["target_port"])) not in retry_pairs)]

    # Atomar neu schreiben: Temp-Datei + Rename.
    tmp = detail_path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_HEADER)
        w.writeheader()
        w.writerows(keep)
    os.replace(tmp, detail_path)

    done = {
        (r["source_ip"], int(r["source_port"]),
         r["target_ip"], int(r["target_port"])) for r in keep
    }
    return done, len(retry_pairs)


class CsvWriter:
    """csv.writer + Zugriff auf die darunterliegende Datei (fuer flush)."""

    def __init__(self, fh):
        self._fh = fh
        self.writer = csv.writer(fh)

    def writerow(self, row) -> None:
        self.writer.writerow(row)

    def flush(self) -> None:
        self._fh.flush()


class Progress:
    """Fortschrittsanzeige mit Resume-Offset und ehrlicher Rate.

    - total   = Umfang des aktuellen Laufs (inkl. bereits fertiger Paare)
    - initial = bereits fertige Paare vor diesem Lauf (Resume-Offset)
    - real    = echte SSH-Tests dieses Laufs (bestimmen die Rate)
    - instant = sofort abgeschlossene Paare (z.B. source_unreachable),
                zaehlen fuer die Bar-Position, aber nicht fuer die Rate.
    - counts  = Live-Zaehler je Status (farbig im Postfix)
    """

    BAR_FMT = "{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} [{elapsed}, {postfix}]"

    def __init__(self, total: int, initial: int = 0):
        self.total = total
        self.initial = initial
        self.real = 0
        self.instant = 0
        self.counts = Counter()
        self.lock = threading.Lock()
        self.start = time.monotonic()
        self.tq = (
            tqdm(total=total, initial=initial, desc="SSH-Tests", unit="test",
                 file=sys.stderr, leave=True, mininterval=1.0, bar_format=self.BAR_FMT)
            if tqdm else None
        )

    def _postfix(self) -> str:
        elapsed = time.monotonic() - self.start
        rate = self.real / elapsed if elapsed > 0 and self.real else 0.0
        remaining = self.total - self.initial - self.real - self.instant
        eta = int(remaining / rate) if rate > 0 and remaining > 0 else 0
        parts = []
        if rate > 0:
            parts.append(f"{rate:.1f} real/s")
        if self.instant:
            parts.append(f"{self.instant} instant")
        if eta:
            parts.append(f"ETA {eta}s")
        for st in STATUS_ORDER:
            n = self.counts.get(st, 0)
            if n:
                parts.append(colored(st, f"{STATUS_SHORT[st]}:{n}"))
        return ", ".join(parts) if parts else "keine Tests bisher"

    def update(self, n: int = 1, instant: bool = False, status: str = None) -> None:
        with self.lock:
            if instant:
                self.instant += n
            else:
                self.real += n
            if status:
                self.counts[status] += n
            if self.tq:
                self.tq.update(n)
                self.tq.set_postfix_str(self._postfix())
            elif (self.real + self.instant) % 500 == 0:
                print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] "
                      f"{self.initial + self.real + self.instant}/{self.total} "
                      f"({self._postfix()})", file=sys.stderr)

    def close(self) -> None:
        if self.tq:
            self.tq.close()


def chunk_list(lst: list, n: int) -> list:
    if n <= 1:
        return [lst]
    k = max(1, -(-len(lst) // n))
    return [lst[i:i + k] for i in range(0, len(lst), k)]


def worker(src, targets, args, password, writer, lock, progress,
           direction_working, log) -> tuple:
    tester = SourceTester(src, args.user, password, args.timeout)
    ok, err = tester.connect()
    if not ok:
        with lock:
            for tgt in targets:
                if args.subnet_quota > 0:
                    direction = (src["net"], tgt["net"])
                    if len(direction_working[direction]) >= args.subnet_quota:
                        writer.writerow(make_row(src, tgt, "quota_skip", "skipped", 0, ""))
                        progress.update(1, instant=True, status="skipped")
                        continue
                writer.writerow(make_row(src, tgt, "connect", "source_unreachable", 0, err))
                progress.update(1, instant=True, status="source_unreachable")
            writer.flush()
        log.warning("Quelle %s:%s nicht erreichbar: %s", src["ip"], src["port"], err)
        return (src["ip"], src["port"]), len(targets), 0

    tester.detect_tools()
    log.info("Quelle %s:%s verbunden, Tools: %s", src["ip"], src["port"],
             {k: v for k, v in tester.tools.items() if v})

    consec = 0
    done = 0
    while done < len(targets):
        tgt = targets[done]

        # Subnetz-Quota: Richtung schon ausreichend getestet?
        if args.subnet_quota > 0:
            direction = (src["net"], tgt["net"])
            with lock:
                if len(direction_working[direction]) >= args.subnet_quota:
                    writer.writerow(make_row(src, tgt, "quota_skip", "skipped", 0, ""))
                    writer.flush()
                    progress.update(1, instant=True, status="skipped")
                    done += 1
                    continue

        res = tester.test_target(tgt)
        with lock:
            writer.writerow(make_row(src, tgt, res["method"], res["status"],
                                     res["latency_ms"], res["error"]))
            writer.flush()
            progress.update(1, status=res["status"])
            if args.subnet_quota > 0 and res["status"] == "auth_ok":
                direction_working[(src["net"], tgt["net"])].add(src["ip"])
        if res["status"] == "tool_error" and not res["error"]:
            consec += 1
        else:
            consec = 0
        if consec >= 3:
            log.warning("Quelle %s:%s: Verbindung scheint tot, Reconnect", src["ip"], src["port"])
            ok, err = tester.reconnect()
            if not ok:
                rest = targets[done + 1:]
                if rest:
                    with lock:
                        for r in rest:
                            writer.writerow(make_row(src, r, "connect",
                                                     "source_unreachable", 0, err))
                        writer.flush()
                        progress.update(len(rest), instant=True, status="source_unreachable")
                    log.warning("Reconnect fehlgeschlagen, %d Ziele als "
                                "source_unreachable markiert", len(rest))
                break
            consec = 0
        done += 1

    tester.cleanup()
    return (src["ip"], src["port"]), done, 0


# ---------------------------------------------------------------- Main

def parse_args():
    ap = argparse.ArgumentParser(
        description="SSH-Matrix-Tester: prueft fuer alle IP-Paare, ob Quelle A "
                    "per SSH-Login Ziel B erreichen kann (beide Richtungen).")
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
    ap.add_argument("--subnet-gap", type=int, default=16,
                    help="Luecken-Schwellwert fuer Subnetz-Clustering (Default: 16). "
                         "0 = feste /24 wie bisher")
    ap.add_argument("--limit-pairs", type=int, default=0,
                    help="Nur die ersten N Paare testen (Dry-Run/Trockentest)")
    return ap.parse_args()


def resolve_password(args) -> str:
    if args.pass_file:
        with open(args.pass_file, "r", encoding="utf-8") as fh:
            pw = fh.readline().rstrip("\r\n")
        if not pw:
            print(f"FEHLER: --pass-file {args.pass_file} ist leer", file=sys.stderr)
            sys.exit(2)
        return pw
    env = args.pass_env or "SSHPASS"
    pw = os.environ.get(env)
    if pw is None:
        print(f"FEHLER: Umgebungsvariable {env} ist nicht gesetzt "
              f"(export {env}='...')", file=sys.stderr)
        sys.exit(2)
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
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(ColorFormatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(fh)
    log.addHandler(sh)

    hosts = parse_ips_file(args.ips, args.port_default, log)
    if not hosts:
        log.error("Keine gueltigen IPs in %s gefunden", args.ips)
        sys.exit(1)
    cluster_subnets(hosts, args.subnet_gap)
    nets = sorted(set(h["net"] for h in hosts))
    log.info("%d Endpunkte geladen, %d Subnetze (gap=%d)", len(hosts), len(nets),
             args.subnet_gap)
    if args.subnet_quota > 0:
        log.info("--subnet-quota %d: pro Richtung werden nach %d erfolgreichen "
                 "Quell-Hosts die restlichen Paare als 'skipped' uebersprungen",
                 args.subnet_quota, args.subnet_quota)

    pairs = [
        (a, b) for a in hosts for b in hosts
        if not (a["ip"] == b["ip"] and a["port"] == b["port"])
    ]
    log.info("%d geordnete Paare (beide Richtungen)", len(pairs))
    if args.limit_pairs:
        pairs = pairs[:args.limit_pairs]
        log.info("--limit-pairs: teste nur %d Paare", len(pairs))
    pairs_before_filter = list(pairs)

    detail_path = os.path.join(args.out, "detail.csv")

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

    if retry_statuses:
        current_keys = {pair_key(p) for p in pairs}
        done, pruned = prune_detail(detail_path, retry_statuses, current_keys, log)
        before = len(pairs)
        pairs = [p for p in pairs if pair_key(p) not in done]
        log.info("--retry-status: %d Paare mit Status %s zum Re-Test markiert, "
                 "%d verbleiben insgesamt", pruned, ",".join(sorted(retry_statuses)),
                 len(pairs))
    elif args.resume:
        done = load_completed(detail_path)
        before = len(pairs)
        pairs = [p for p in pairs if pair_key(p) not in done]
        log.info("--resume: %d Paare bereits getestet, %d verbleiben",
                 before - len(pairs), len(pairs))
    else:
        done = set()

    initial = sum(1 for p in pairs_before_filter if pair_key(p) in done)
    total = len(pairs_before_filter)

    if not pairs:
        log.info("Keine Paare mehr zu testen. Report: "
                 "python3 ssh_matrix_report.py --detail %s --out %s", detail_path, args.out)
        sys.exit(0)

    by_source = {}
    for src, tgt in pairs:
        by_source.setdefault((src["ip"], src["port"]), []).append(tgt)
    src_lookup = {(h["ip"], h["port"]): h for h in hosts}

    tasks = []
    for key, targets in by_source.items():
        for chunk in chunk_list(targets, args.per_source):
            tasks.append((src_lookup[key], chunk))

    write_mode = "a" if (os.path.exists(detail_path) and os.path.getsize(detail_path) > 0) else "w"
    csvf = open(detail_path, write_mode, newline="", encoding="utf-8")
    writer = CsvWriter(csvf)
    if write_mode == "w":
        writer.writerow(CSV_HEADER)
        writer.flush()
    lock = threading.Lock()
    progress = Progress(total=total, initial=initial)
    direction_working = defaultdict(set)  # (src_net, tgt_net) -> set von Quell-IPs mit auth_ok
    if args.subnet_quota > 0 and os.path.exists(detail_path):
        with open(detail_path, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if r["status"] == "auth_ok":
                    s_net = r.get("src_net") or r.get("src_24", "")
                    t_net = r.get("tgt_net") or r.get("tgt_24", "")
                    if s_net and t_net:
                        direction_working[(s_net, t_net)].add(r["source_ip"])
        met = sum(1 for v in direction_working.values() if len(v) >= args.subnet_quota)
        log.info("--subnet-quota: %d Richtungen aus detail.csv geladen (%d bereits erfuellt)",
                 len(direction_working), met)

    log.info("Start: %d Aufgaben, %d Worker, timeout=%ds, per-source=%d",
             len(tasks), args.workers, args.timeout, args.per_source)

    started = time.monotonic()
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = {ex.submit(worker, s, t, args, password, writer, lock,
                                 progress, direction_working, log): s for s, t in tasks}
            for fut in as_completed(futures):
                try:
                    fut.result()
                except Exception as exc:
                    log.exception("Worker-Fehler fuer Quelle %s", futures[fut])
    except KeyboardInterrupt:
        log.warning("Abbruch durch Benutzer - bereits geschriebene Ergebnisse bleiben erhalten "
                    "(Resume mit --resume moeglich)")
        csvf.flush()
        csvf.close()
        progress.close()
        sys.exit(130)

    csvf.close()
    progress.close()
    elapsed = time.monotonic() - started

    log.info("Fertig in %ds. Auswertung: python3 ssh_matrix_report.py --detail %s --out %s",
             int(elapsed), detail_path, args.out)
    ok_count = progress.counts.get("auth_ok", 0)
    fail_count = progress.real + progress.instant - ok_count
    if USE_COLOR:
        print(f"\n{C_GREEN}Fertig{C_RESET} in {int(elapsed)}s. "
              f"{colored('auth_ok', f'{ok_count} OK')}, "
              f"{colored('auth_fail', f'{fail_count} Fehler')}. "
              f"Ergebnisse: {detail_path}", file=sys.stderr)
        print(f"{C_CYAN}Report erzeugen:{C_RESET}", file=sys.stderr)
        print(f"  python3 ssh_matrix_report.py --detail {detail_path} --out {args.out}",
              file=sys.stderr)
    else:
        print(f"\nFertig in {int(elapsed)}s. {ok_count} OK, {fail_count} Fehler. "
              f"Ergebnisse: {detail_path}", file=sys.stderr)
        print("Report erzeugen:", file=sys.stderr)
        print(f"  python3 ssh_matrix_report.py --detail {detail_path} --out {args.out}",
              file=sys.stderr)


if __name__ == "__main__":
    main()
