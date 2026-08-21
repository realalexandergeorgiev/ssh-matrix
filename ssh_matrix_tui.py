#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ssh_matrix_tui.py - Textual-TUI fuer den SSH-Matrix-Tester (v2.0.0).

Panes: Status, Settings, Graphen (btop-Look: pairs/s, real-tests/s,
aktive Threads) und Log. Stop/Quit nur nach Bestaetigungs-Modal.
Lazy-Import: nur mit --tui bzw. Auto-Detect (TTY) geladen; der
CLI-Modus kommt ohne textual aus.
"""

import logging
import queue
import threading
import time

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (DataTable, Footer, Header, Input, ProgressBar,
                             RichLog, Sparkline, Static)

from ssh_matrix import (QUOTA_MODES, STATUS_ORDER, STATUS_SHORT,
                        STATUS_COLORS, VERSION, colored, fmt_duration,
                        fmt_num, interim_report_lines, set_workers)


# ---------------------------------------------------------------- Log-Bruecke

class TuiLogHandler(logging.Handler):
    """Logging-Handler, der Zeilen in eine Thread-sichere Queue legt.
    Die TUI leert die Queue im Refresher in das Log-Pane."""

    def __init__(self, maxlen: int = 1000):
        super().__init__()
        self.messages = queue.Queue(maxlen)

    def emit(self, record):
        try:
            self.messages.put_nowait(self.format(record))
        except Exception:
            pass


# ---------------------------------------------------------------- Modals

class ConfirmModal(ModalScreen[bool]):
    """Abbrechen-Warnung: Stop/Quit nur nach Bestaetigung."""

    BINDINGS = [
        Binding("j", "yes", "Ja"),
        Binding("n", "no", "Nein"),
        Binding("escape", "no", "Nein"),
    ]

    def __init__(self, title: str, message: str):
        super().__init__()
        self._title = title
        self._message = message

    def compose(self) -> ComposeResult:
        yield Static(f"[b]{self._title}[/b]\n\n{self._message}\n\n"
                     f"[b]Wirklich fortfahren?[/b]  [green]j[/green]=Ja  "
                     f"[yellow]n[/yellow]/[yellow]Esc[/yellow]=Nein",
                     id="confirm")

    def action_yes(self):
        self.dismiss(True)

    def action_no(self):
        self.dismiss(False)


class SettingsModal(ModalScreen):
    """Eingabe-Modal fuer eine Settings-Aenderung.

    WICHTIG: Das fokussierte Input verschluckt die Enter-Taste (Widget-
    Bindings haben Vorrang vor Screen-Bindings) - daher wird Enter ueber
    das Input.Submitted-Event abgefangen, nicht ueber ein Screen-Binding.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Abbrechen"),
    ]

    def __init__(self, key: str, label: str, current: str, on_submit):
        super().__init__()
        self._key = key
        self._label = label
        self._current = current
        self._on_submit = on_submit

    def compose(self) -> ComposeResult:
        yield Static(f"[b]{self._label}[/b] (aktuell: {self._current})")
        yield Input(placeholder=f"Neuer Wert fuer {self._key} ...",
                    id="settings-input")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.input.value.strip()
        if value:
            self._on_submit(self._key, value)
        self.dismiss(None)

    def action_cancel(self):
        self.dismiss(None)


class ReportModal(ModalScreen):
    """Kumulativer Zwischenbericht als scrollbares Overlay."""

    BINDINGS = [Binding("escape", "close", "Schliessen"),
                Binding("j", "close", "Schliessen")]

    def __init__(self, lines):
        super().__init__()
        self._lines = lines

    def compose(self) -> ComposeResult:
        yield RichLog(highlight=True, markup=True, wrap=True, id="report")

    def on_mount(self):
        log = self.query_one("#report", RichLog)
        for line in self._lines:
            log.write(line)

    def action_close(self):
        self.dismiss(None)


# ---------------------------------------------------------------- Haupt-App

class SSHMatrixApp(App):
    """TUI fuer den SSH-Matrix-Tester (btop-artige Panes)."""

    TITLE = f"SSH-Matrix-Tester {VERSION}"
    SUB_TITLE = "Alex & DeepSeek"
    CSS = """
    #status-box { border: round $primary; padding: 0 1; }
    #settings-box { border: round $accent; padding: 0 1; }
    #graphs-box { border: round $secondary; padding: 0 1; }
    #log-box { border: round $warning; padding: 0 1; }
    Sparkline { height: 3; }
    #status-table { height: auto; }
    #status-eta { height: auto; }
    """

    BINDINGS = [
        Binding("s", "stop", "Stop"),
        Binding("r", "report", "Report"),
        Binding("p", "pause", "Pause"),
        Binding("w", "set_workers", "Worker"),
        Binding("t", "set_timeout", "Timeout"),
        Binding("q", "set_quota", "Quota"),
        Binding("m", "set_mode", "Modus"),
        Binding("v", "set_verbose", "Verbosity"),
        Binding("ctrl+c", "quit", "Quit"),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self, config, stats, direction_working, work_queue,
                 stop_event, user, password, writer, lock, log, ctx,
                 log_handler):
        super().__init__()
        self.config = config
        self.stats = stats
        self.direction_working = direction_working
        self.work_queue = work_queue
        self.stop_event = stop_event
        self.user = user
        self.password = password
        self.writer = writer
        self.lock = lock
        self.runlog = log
        self.ctx = ctx
        self.log_handler = log_handler
        self._paused_workers = None
        self._stopping = False

    # -- Aufbau ---------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            with Vertical(id="status-box"):
                yield Static("", id="status-eta")
                yield ProgressBar(total=max(1, self.stats.remaining()),
                                  show_percentage=True, id="status-bar")
                yield DataTable(id="status-table")
            with Vertical(id="settings-box"):
                yield Static("", id="settings")
        with Horizontal():
            with Vertical(id="graphs-box"):
                yield Static("[b]pairs/s[/b]", id="lbl-pps")
                yield Sparkline([0], summary_function=max, id="spark-pps")
                yield Static("[b]real-tests/s[/b]", id="lbl-real")
                yield Sparkline([0], summary_function=max, id="spark-real")
                yield Static("[b]aktive Threads[/b]", id="lbl-threads")
                yield Sparkline([0], summary_function=max, id="spark-threads")
            with Vertical(id="log-box"):
                yield RichLog(highlight=True, markup=True, wrap=False,
                              max_lines=500, id="log")
        yield Footer()

    def on_mount(self):
        table = self.query_one("#status-table", DataTable)
        table.add_columns("Status", "Anzahl", "Bedeutung")
        self.set_interval(0.5, self._refresh)
        self._refresh()

    # -- Refresher ------------------------------------------------------

    def _refresh(self):
        try:
            self._do_refresh()
        except Exception:
            import traceback
            tb = traceback.format_exc()
            self.runlog.error("TUI-Refresher-Fehler:\n%s", tb)
            try:
                self.query_one("#log", RichLog).write(
                    f"[red]TUI-Refresher-Fehler: {tb.splitlines()[-1]}[/red]")
            except Exception:
                pass

    def _do_refresh(self):
        snap = self.stats.snapshot()
        with self.config.lock:
            active = self.config.active_workers

        # Messpunkte fuer Graphen
        self.stats.sample(active)
        snap = self.stats.snapshot()

        # Status: dieser Lauf (verbleibend) + Gesamt (inkl. Vorlauf)
        done = snap["done"]
        remaining = snap["remaining"]
        this_run = snap["real"] + snap["instant"]
        pct_total = 100.0 * done / snap["total"] if snap["total"] else 0.0
        pct_run = 100.0 * this_run / remaining if remaining > 0 else 0.0
        elapsed = time.monotonic() - snap["start"]
        rate = snap["real"] / elapsed if elapsed > 0 and snap["real"] else 0.0
        eta = fmt_duration(remaining / rate) if rate > 0 and remaining > 0 else "unbekannt"
        if remaining:
            self.query_one("#status-eta", Static).update(
                f"[b]noch {fmt_num(remaining)} Paare[/b]  ·  "
                f"dieser Lauf {fmt_num(this_run)}/{fmt_num(remaining)} "
                f"({pct_run:.2f}%)  ·  {rate:.1f} Tests/s  ·  ETA {eta}\n"
                f"[dim]Gesamt {fmt_num(snap['total'])} "
                f"({fmt_num(done)} getestet, davon {fmt_num(snap['initial'])} "
                f"Vorlauf, {pct_total:.2f}%)[/dim]")
        else:
            self.query_one("#status-eta", Static).update(
                f"[b]Fertig - alle Paare getestet[/b]  ·  {fmt_num(done)} "
                f"von {fmt_num(snap['total'])}")
        bar = self.query_one("#status-bar", ProgressBar)
        bar.total = max(1, remaining)
        bar.progress = min(bar.total, this_run)

        # Status-Tabelle (kumulativ: Vorlauf + Lauf)
        table = self.query_one("#status-table", DataTable)
        table.clear()
        merged = dict(snap["detail_counts"])
        for st, n in snap["counts"].items():
            merged[st] = merged.get(st, 0) + n
        for st in STATUS_ORDER:
            n = merged.get(st, 0)
            if n:
                table.add_row(STATUS_SHORT[st], fmt_num(n),
                              self._status_desc(st),
                              key=st)

        # Settings
        with self.config.lock:
            target = self.config.target_workers
        self.query_one("#settings", Static).update(
            f"[b]Settings[/b]\n"
            f"Worker     {active}/{target}   [dim](w)[/dim]\n"
            f"Timeout    {self.config.timeout}s  [dim](t)[/dim]\n"
            f"Quota      {self.config.subnet_quota}  [dim](q)[/dim]\n"
            f"Modus      {self.config.quota_mode}  [dim](m)[/dim]\n"
            f"Verbosity  {self.config.verbose_level}  [dim](v)[/dim]\n"
            f"\n[dim]s=Stop  r=Report  p=Pause[/dim]")

        # Graphen (EMA-geglaettete Kurven + aktuelle Werte als Zahlen)
        pps_ema = snap["pps_ema"] or [0]
        real_ema = snap["real_ema"] or [0]
        self.query_one("#spark-pps", Sparkline).data = pps_ema
        self.query_one("#spark-real", Sparkline).data = real_ema
        self.query_one("#spark-threads", Sparkline).data = snap["threads_h"] or [0]
        self.query_one("#lbl-pps", Static).update(
            f"[b]pairs/s[/b]  {pps_ema[-1]:.1f}")
        self.query_one("#lbl-real", Static).update(
            f"[b]real-tests/s[/b]  {real_ema[-1]:.1f}")
        self.query_one("#lbl-threads", Static).update(
            f"[b]aktive Threads[/b]  {active}")

        # Log-Pane leeren
        if self.log_handler is not None:
            while True:
                try:
                    line = self.log_handler.messages.get_nowait()
                except queue.Empty:
                    break
                self.query_one("#log", RichLog).write(
                    self._colorize_log(line))

    @staticmethod
    def _status_desc(st: str) -> str:
        from ssh_matrix import STATUS_DESCRIPTIONS
        return STATUS_DESCRIPTIONS.get(st, st)

    @staticmethod
    def _colorize_log(line: str) -> str:
        """Level-Wort einfarben (INFO gruen, WARNING gelb, ERROR rot)."""
        if " WARNING " in line:
            return line.replace(" WARNING ", " [yellow]WARNING[/] ", 1)
        if " ERROR " in line:
            return line.replace(" ERROR ", " [red]ERROR[/] ", 1)
        if " INFO " in line:
            return line.replace(" INFO ", " [green]INFO[/] ", 1)
        return line

    # -- Aktionen -------------------------------------------------------

    def _stop_requested(self):
        self._stopping = True
        self.stop_event.set()
        self.runlog.warning("Stop angefordert (TUI) - Worker beenden aktuellen "
                         "Test, Rest bleibt fuer --resume erhalten")
        self.exit()

    def action_stop(self):
        if self._stopping:
            return
        self.push_screen(ConfirmModal(
            "Stop", "Der Lauf wird sauber beendet; nicht getestete Paare "
                    "bleiben fuer --resume erhalten."),
            callback=lambda ok: self._stop_requested() if ok else None)

    def action_quit(self):
        if self._stopping:
            return
        self.push_screen(ConfirmModal(
            "Beenden", "Der Lauf wird sauber beendet; nicht getestete Paare "
                       "bleiben fuer --resume erhalten."),
            callback=lambda ok: self._stop_requested() if ok else None)

    def action_report(self):
        self.push_screen(ReportModal(
            interim_report_lines(self.stats, self.direction_working,
                                 self.config)))

    def action_pause(self):
        if self._paused_workers is None:
            self._paused_workers = self.config.target_workers
            set_workers(self.config, 0, self.work_queue, self.stop_event,
                        self.user, self.password, self.writer, self.lock,
                        self.stats, self.direction_working, self.runlog, self.ctx)
            self.runlog.warning("Pause - Worker beenden aktuelle Tests")
        else:
            n = self._paused_workers
            self._paused_workers = None
            set_workers(self.config, n, self.work_queue, self.stop_event,
                        self.user, self.password, self.writer, self.lock,
                        self.stats, self.direction_working, self.runlog, self.ctx)
            self.runlog.warning("Weiter - Worker-Ziel wieder auf %d", n)

    def _settings_modal(self, key, label, current, validate):
        self.push_screen(SettingsModal(key, label, current,
                                       lambda k, v: self._apply_setting(k, v, validate)))

    def _apply_setting(self, key, value, validate):
        try:
            parsed = validate(value)
        except ValueError:
            self.runlog.warning("Ungueltiger Wert fuer %s: %r", key, value)
            return
        if key == "w":
            set_workers(self.config, parsed, self.work_queue, self.stop_event,
                        self.user, self.password, self.writer, self.lock,
                        self.stats, self.direction_working, self.runlog, self.ctx)
            self.runlog.warning("Worker-Ziel auf %d gesetzt", parsed)
        elif key == "t":
            self.config.timeout = parsed
            self.runlog.warning("Timeout auf %ds gesetzt", parsed)
        elif key == "q":
            self.config.subnet_quota = parsed
            self.runlog.warning("Subnetz-Quota auf %d gesetzt", parsed)
        elif key == "m":
            self.config.quota_mode = parsed
            self.runlog.warning("Quota-Modus auf %s gesetzt", parsed)
        elif key == "v":
            self.config.verbose_level = parsed
            self.runlog.warning("Verbosity auf %s gesetzt", parsed)

    def action_set_workers(self):
        self._settings_modal("w", "Worker-Anzahl", str(self.config.target_workers),
                             lambda v: max(0, int(v)))

    def action_set_timeout(self):
        self._settings_modal("t", "Timeout (Sekunden)", str(self.config.timeout),
                             lambda v: max(1, int(v)))

    def action_set_quota(self):
        self._settings_modal("q", "Subnetz-Quota", str(self.config.subnet_quota),
                             lambda v: max(0, int(v)))

    def action_set_mode(self):
        self._settings_modal("m", "Quota-Modus (auth_ok|reachable)",
                             self.config.quota_mode,
                             lambda v: self._check_mode(v))

    @staticmethod
    def _check_mode(v: str) -> str:
        v = v.strip().lower()
        if v not in QUOTA_MODES:
            raise ValueError(v)
        return v

    def action_set_verbose(self):
        self._settings_modal("v", "Verbosity (err|warn|info)",
                             self.config.verbose_level,
                             lambda v: self._check_verbose(v))

    @staticmethod
    def _check_verbose(v: str) -> str:
        from ssh_matrix import LOG_LEVELS
        v = v.strip().lower()
        if v not in LOG_LEVELS:
            raise ValueError(v)
        return v


# ---------------------------------------------------------------- Einstieg

def run_tui(config, stats, direction_working, work_queue, stop_event,
            user, password, writer, lock, log, ctx, log_handler) -> None:
    """Blockiert bis Stop/Quit (bestaetigt per Modal)."""
    SSHMatrixApp(config, stats, direction_working, work_queue, stop_event,
                 user, password, writer, lock, log, ctx, log_handler).run()
