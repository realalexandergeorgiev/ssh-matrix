# Changelog

Alle nennenswerten Änderungen am SSH-Matrix-Tester.

Das Format folgt [Keep a Changelog](https://keepachangelog.com/de/1.1.0/).
Dieses Projekt verwendet [Semantic Versioning](https://semver.org/lang/de/).

## [v2.0.1] (2026-08-20) — TUI-Fixes: Settings-Enter, Graphen, Resume-Anzeige

### Behoben
- **Settings ändern in der TUI ging nicht**: Das fokussierte `Input`
  verschluckte die Enter-Taste (Widget-Bindings haben Vorrang vor
  Screen-Bindings) — `action_ok` wurde nie aufgerufen. Enter wird jetzt
  über das `Input.Submitted`-Event abgefangen und übernimmt den Wert.
- **Graphen (pairs/s, real-tests/s, aktive Threads) waren statisch**:
  `RunStats.sample()` erzeugte Spike-Plus-Null-Serien (z. B. ein
  135-Mio.-Ausreißer + lauter 0), die Sparkline skalierte auf den Spike
  → fast alles eine flache Null-Linie. Jetzt **EMA-geglättete Serien**
  (`pps_ema`/`real_ema`, α=0,3) für sichtbare Kurven, zusätzlich die
  aktuellen Werte als Zahlen neben den Graphen.
- **Resume-Anzeige irreführend**: Der Fortschrittsbalken zeigte Gesamt
  inkl. Vorlauf (z. B. 12,6 Mio./12,6 Mio. = 99,79 %), obwohl noch
  Tausende Paare offen waren. Jetzt zeigt der Balken **diesen Lauf**
  (0 → verbleibend) mit großer Zeile „noch X Paare" und Sekundärzeile
  „Gesamt · Vorlauf · offen". Der CLI-Einzeiler nennt die offenen Paare
  ebenfalls („noch X").

## [v2.0.0] (2026-08-20) — TUI (Textual) & vereinfachter CLI

### Hinzugefügt
- **TUI (Textual, btop-artig)** — Auto-Detect (TTY + textual installiert),
  erzwingbar mit `--tui`, deaktivierbar mit `--no-tui`:
  - **Panes**: Status (Fortschritt, ETA, kumulative Status-Tabelle),
    Settings (Worker/Timeout/Quota/Modus/Verbosity), Graphen
    (pairs/s, real-tests/s, aktive Threads als Sparklines), Log
    (farbige Zeilen via `TuiLogHandler`)
  - **Tasten**: `s` Stop, `q` Quit, `r` Report-Overlay (kumulativer
    Zwischenbericht), `p` Pause/Weiter, `w/t/q/m/v` Settings-Eingabe
    (Modal), `ctrl+c`/`ctrl+q` Beenden
  - **Abbrechen-Warnung**: Stop/Quit nur nach Bestätigungs-Modal
    (`j`=Ja, `n`/`Esc`=Nein)
- **CLI vereinfacht**: tqdm und das Pause-Menü sind entfernt. CLI zeigt
  Log-Zeilen + **periodischen Status-Einzeiler** (`--status-interval`,
  Default 30 s): `[14:02:30] 18.342/12.648.692 (0,15 %) · 2,3/s · ETA 2h 05m`.
- **1× Ctrl+C im CLI = sauberer Stop** (Worker beenden aktuellen Test,
  Resume-fähig, Exit 0); 2× Ctrl+C = hart (Exit 130).
- Neues Modul `ssh_matrix_tui.py` (lazy import — CLI braucht kein textual).

### Geändert
- `Progress` (tqdm) → **`RunStats`**: thread-sichere Statistik mit
  Historie-Deques (pps, real/s, Threads) für Graphen; `snapshot()`,
  `sample()`, `status_line()`. tqdm ist **keine Abhängigkeit mehr**.
- `print_interim_report` → `interim_report_lines()` (Zeilenliste, von der
  TUI als Overlay gerendert).

## [v1.2.6] (2026-08-20) — Statuszeile stabil & Zwischenbericht kumulativ

### Behoben
- **Statuszeile (Fortschrittsbalken) verschwand nach dem Pause-Menü**:
  Das Menü druckte auf stderr, während die tqdm-Bar dort lebte → nach
  `c` zeichnete tqdm an der falschen Position neu. Jetzt blenden
  `Progress.hide()`/`show()` die Bar vor dem Menü aus und bauen sie
  danach mit erhaltenem Zählerstand neu auf.
- **Zwischenbericht zeigte „viel 0"**: Er zeigte nur die Zähler des
  aktuellen Laufs und rundete Prozent auf 0 Nachkommastellen (bei großen
  Listen 0%). Jetzt kumulativ:
  - Status-Verteilung **gesamt (Vorlauf aus detail.csv + Lauf)**
  - Zeile „davon X bereits getestet (Vorlauf), Y in diesem Lauf"
  - Prozent mit **2 Nachkommastellen** (0,15 % statt 0 %)
  - Zahlen mit Punkt-Tausendertrennung (12.648.692)
- **Lade-Phase sichtbar**: Beim Laden großer detail.csv wird „detail.csv
  laden …" und „… geladen in Xs (N getestete Paare)" geloggt — die Bar
  erscheint erst danach, das ist jetzt nachvollziehbar.

### Hinzugefügt
- `detail_counts`-Counter in `stream_detail` (Status-Verteilung der
  Vorlauf-Daten, ein Durchlauf), im Zwischenbericht genutzt.

## [v1.2.5] (2026-08-20) — Ctrl+C im Pause-Menü korrigiert

### Behoben
- Ein einzelnes Ctrl+C **im Pause-Menü** brach bisher sofort hart ab
  („Hart abgebrochen.", Exit 130) — obwohl der Nutzer nur wieder ins
  Menü wollte. Jetzt zeigt das erste Ctrl+C im Menü nur einen Hinweis
  und lässt das Menü offen; erst **2× Ctrl+C hintereinander** bricht
  hart ab (Exit 130). Jede normale Eingabe setzt den Zähler zurück.

## [v1.2.4] (2026-08-20) — Verbosity im Pause-Menü

### Hinzugefügt
- Menü-Befehl `v LEVEL` (`err|warn|info`): ändert den Detailgrad der
  Terminal-Ausgabe zur Laufzeit (wirkt sofort auf stderr; `run.log`
  bleibt immer vollständig). Ungültige Stufen werden abgelehnt.

## [v1.2.3] (2026-08-20) — RAM-Optimierung (Streaming statt O(n²))

### Behoben
- **Riesiger RAM-Verbrauch bei großen IP-Listen**: Für 3557 Endpunkte
  (12,6 Mio. Paare) stieg der Speicher auf mehrere GB und blieb dort.
  Vier O(n²)-Strukturen wurden eliminiert:
  - `pairs`-Liste (2er-Tupel, ~800 MB) → entfällt komplett; Worker
    iterieren die Hosts direkt (Tasks sind nur noch `(src_id, id_range)`)
  - `pairs_before_filter`-Kopie (~100 MB) → entfällt; `total` wird
    arithmetisch berechnet, `initial` im Streaming-Pass mitgezählt
  - `done`-Set aus `detail.csv` bei `--resume` (~2–3 GB) → **Bitmap**
    (`PairBits`, 1 Bit/Paar): bei 3557 Endpunkten nur ~1,6 MB
  - `by_source`-Ziel-Listen/`tasks`/Queue (~200–400 MB) → nur noch n
    kleine Tasks
- **Gemessen (3557 Endpunkte, separater Prozess):** vorher ~1000 MB,
  nachher **~34 MB Peak** (inkl. Python/paramiko-Basis).
- `direction_working` (Subnetz-Quota) wird auf `quota` Einträge pro
  Richtung gekappt (nur die Länge zählt) — auch beim Laden aus
  `detail.csv`. Mit `--subnet-quota 1`: praktisch 0 RAM.
- `prune_detail` (Retry) lädt keine Zeilenliste mehr, sondern streamt:
  Retry-Paare als drittes Bitmap, Neu-Schreiben in einem Pass.
- `--limit-pairs` als `in_scope`-Bitmap (gleiche Semantik: erste N Paare
  in a-, dann b-Reihenfolge).

### Hinzugefügt
- **RAM-Warnung**: `estimate_ram_mb()` schätzt den Spitzenverbrauch
  (Streaming-Architektur, linear statt n²). Über der Schwelle
  (Default 1 GB, per `SSH_MATRIX_RAM_WARN_MB` anpassbar) erscheint eine
  WARNING und es wird explizit gefragt: „Trotzdem fortfahren? [j/N]".
  Ohne Terminal (Pipe) wird abgebrochen; `--force` überschreibt die
  Nachfrage.

## [v1.2.2] (2026-08-20) — Optionaler Log-Detailgrad (--verbose)

### Hinzugefügt
- Neues Flag `--verbose {err,warn,info}` (Default `info`): steuert den
  Detailgrad der Terminal-Ausgabe (stderr). `err` = nur Fehler, `warn` =
  Fehler + Warnungen, `info` = alles.
- `run.log` bleibt **immer vollständig** (INFO) — Diagnose unabhängig von
  der Terminal-Einstellung.
- Sinnvoll, wenn Warn-/Info-Zeilen das Pause-Menü stören: mit
  `--verbose err` bleibt das Menü ungestört.

## [v1.2.1] (2026-08-20) — Versions-Banner

### Hinzugefügt
- Start-Banner in `ssh_matrix.py` und `ssh_matrix_report.py` mit Version
  (v1.2.1) und Autoren-Hinweis (Alex & DeepSeek), farbig bei TTY
- `--version`-Flag in beiden Skripten
- Version/Autoren-Hinweis in der README

## [v1.2.0] (2026-08-20) — Pause-Menü zur Laufzeit

### Hinzugefügt
- **Pause-Menü** per 1× Ctrl+C während des Laufs (Worker laufen weiter).
  2× Ctrl+C bricht hart ab (Exit 130).
  - `s` — sauberer Stop: Worker beenden den aktuellen Test, restliche Paare
    bleiben ungeschrieben → `--resume` testet sie später nach
  - `r` — **sprechender, farbiger Zwischenbericht**: Fortschritt + ETA,
    Status-Verteilung mit Klartext-Erklärung je Status (nicht nur Kürzel),
    Subnetz-Erreichbarkeit (bestätigte Richtungen vs. Lücken)
  - `w N` — Worker-Anzahl zur Laufzeit ändern (spawnen/abbauen)
  - `t N` — Timeout zur Laufzeit ändern (wirkt auf neue Tests/Connects)
  - `q N` — Subnetz-Quota zur Laufzeit ändern (wirkt auf ungetestete Paare)
  - `m MODE` — Quota-Modus `auth_ok|reachable` zur Laufzeit ändern
  - `c` — weiterlaufen
- **Worker-Pool umgebaut**: `ThreadPoolExecutor` (fixe Thread-Zahl) durch
  Queue + daemon-Client-Threads ersetzt — Grundlage für dynamische
  Worker-Anzahl und sauberes Stoppen. `RunConfig` hält die veränderbaren
  Parameter; `SourceTester.timeout` ist eine Property, die live aus der
  Config liest.
- `stop_event` im Worker-Loop: beim Stop wird nach dem aktuellen Test
  abgebrochen, SSH-Verbindungen werden sauber geschlossen.

## [v1.1.1] (2026-08-20) — Hängende Worker & NOTOOL-Fehldiagnose behoben

### Behoben
- **`run()` hing unbegrenzt**: `chan.exec_command()` blockiert in paramiko 2.12
  ohne Timeout (`_wait_for_event` → `event.wait()`). Wenn der Server nicht
  antwortete, hing der Worker für immer und belegte einen Thread-Pool-Slot.
  Jetzt wird `exec_command` in einem Helper-Thread mit `join(timeout=30)`
  ausgeführt; bei Timeout wird der Kanal geschlossen und `exec_command timeout`
  zurückgegeben.
- **Ctrl+C hing ebenfalls**: Der `ThreadPoolExecutor`-Kontextmanager wartete
  beim Verlassen auf alle laufenden Futures (`shutdown(wait=True)`). Der
  KeyboardInterrupt-Handler beendet jetzt sofort per `os._exit(130)`
  (CSV/Progress werden vorher geflusht). Kein Warten auf hängende Worker mehr.
- **NOTOOL-Fehldiagnose**: Eine transient fehlgeschlagene Tool-Erkennung
  (leere Probe-Ausgabe, z. B. `Channel closed` unter Last) wurde **dauerhaft
  gecacht** → alle Ziele der Quelle wurden fälschlich `NOTOOL`.
  - Probe wird jetzt bis zu 3× wiederholt.
  - Fehlgeschlagene Erkennung wird **nicht mehr gecacht** (nächster Test
    probiert erneut).
  - **WARNING ins `run.log`** mit gekürzter Probe-Ausgabe, wenn keine Tools
    erkannt wurden → NOTOOL sofort diagnostizierbar.
- **`setsid`-Zwang entfernt**: Der askpass-Pfad (voller SSH-Login) erforderte
  `ssh` **und** `setsid`. Minimal-Systeme/Appliances mit `ssh` (dropbear)
  aber ohne `setsid`/`nc`/`bash` landeten fälschlich bei `NOTOOL`. Da
  paramiko-`exec_command` kein TTY hat, funktioniert der SSH_ASKPASS-Trick
  auch ohne `setsid` (nur optional vorangestellt, wenn vorhanden).
- **`base64`-Abhängigkeit entfernt**: Das askpass-Skript wird jetzt per
  `printf %s` angelegt (POSIX) statt `base64 -d` — kein base64 auf den
  Zielen mehr nötig.
- **Exaktes Zeilen-Matching in der Tool-Erkennung**: `"HAVE:ssh" in text`
  matchte auch `"HAVE:sshpass"` (Substring-False-Positive). Jetzt
  zeilenbasiert (`f"HAVE:{name}" in text.splitlines()`).
- **Drain-Loops in `run()`**: Lesen bis EOF mit Grace-Fenster statt
  sofortigem Abbruch bei Timeout — verhindert Verlust von Rest-Ausgabe.
- **Transport-Keepalive**: `transport.set_keepalive(10)` nach dem Verbinden —
  tote/halboffene Verbindungen werden erkannt statt unbegrenzt zu hängen.
- **Batch-Progress für unerreichbare Quellen**: Die Massenmarkierung
  (`source_unreachable`/`skipped`) aller Ziele einer Quelle aktualisiert den
  Fortschrittsbalken jetzt als **einen Sprung** pro Status statt 219× +1 —
  die Anzeige zeigt sofort den Gesamtbetrag des Bursts.

## [v1.1.0] (2026-08-20) — Quota-Modus wählbar

### Hinzugefügt
- Neues Flag `--quota-mode {auth_ok,reachable}` (Default `auth_ok`):
  bestimmt, welche Testergebnisse für `--subnet-quota` als
  „funktionierender Quell-Host" zählen
  - `auth_ok`: nur voller SSH-Login
  - `reachable`: netzwerkseitig erreichbar (`auth_ok`, `auth_fail` oder
    `port_open`)
- Log-Meldung bei Start zeigt Modus + zählende Status

### Geändert
- Quota-Zähler (`direction_working`) lädt aus `detail.csv` jetzt nur noch
  Status, die im gewählten Modus zählen

## [v1.0.0] (2026-08-20) — GitHub-Release

### Hinzugefügt
- Dokumentation: `README.md`, `CHANGELOG.md`, `ANFORDERUNGEN.md`,
  `ENTSCHEIDUNGEN.md`
- `.gitignore`, Git-Repository initialisiert
- Keine funktionalen Änderungen gegenüber v0.5.0

## [v0.5.0] (2026-08-20) — Subnetz-Quota & Netz-Matrix

### Hinzugefügt
- Neues Flag `--subnet-quota N`: pro Richtung (`src_net → tgt_net`) werden
  nach N erfolgreichen Quell-Hosts die restlichen Paare als `skipped`
  übersprungen
- Neues Flag `--subnet-gap N` (Default 16): intelligentes Subnetz-Grouping
  per Gap-Clustering (Netze werden aus der IP-Verteilung geschätzt, nicht
  starr als /24 angenommen); `--subnet-gap 0` = feste /24 (abwärtskompatibel)
- Neuer Status `skipped` (Methode `quota_skip`)
- Quota-Zähler (`direction_working`) wird aus `detail.csv` geladen — bei
  `--resume` bleiben bereits bestätigte Richtungen übersprungen
- Quota-Check auch bei unerreichbaren Quellen: `skipped` statt
  `source_unreachable`, wenn die Richtung bereits erfüllt ist
- Neues Sheet **Netz-Matrix** in `report.xlsx` + `netz_matrix.csv`:
  Subnetz-Pivot (Zeilen = Quell-Netze, Spalten = Ziel-Netze) mit `ok/tested`
  und Farben (grün = auth_ok vorhanden, gelb = nur Port, rot = alle failed,
  grau = nicht getestet/skipped)
- `--retry-status skipped` zum gezielten Re-Test übersprungener Paare
- Neue CSV-Spalten `src_net`/`tgt_net`; Sheet „Quelle" um Netz-Spalte,
  Sheet „Subnetze" um skipped-Spalte ergänzt

### Geändert
- `--retry-all-failed` retryt `skipped` nicht mehr (kein Fehler, nur nicht
  getestet)
- Legende im Sheet „Suche" um `SKIP` ergänzt

## [v0.4.0] (2026-08-20) — Farbige Ausgabe

### Hinzugefügt
- ANSI-Farben für Log-Zeilen: `verbunden` = grün, `nicht erreichbar` = rot,
  Warnungen = gelb, Fehler = rot + fett, `Start:` = cyan
- Live-Status-Zähler im Fortschrittsbalken-Postfix mit Farben:
  `OK` grün, `AUTH` gelb, `UNREACH`/`SRCERR`/`ERR` rot, `PORT` blau,
  `CLOSED`/`?` grau, `SKIP` grau
- Farbige Zusammenfassung am Laufende („Fertig" grün, OK/Fehler-Zahlen)

### Geändert
- `ColorFormatter` nur für stderr; `run.log` bleibt ungefärbt
- Farben nur wenn stderr ein Terminal ist; `NO_COLOR=1` deaktiviert
  (Auto-Detect via `sys.stderr.isatty()`)

## [v0.3.0] (2026-08-20) — Progress-Bar-Fix

### Geändert
- Resume-Offset: Fortschrittsbalken startet bei `done/total` statt
  `0/remaining` (z. B. `18000/48000` bei 18.000 bereits getesteten Paaren)
- Eigene Rate (`real/s`) nur aus echten SSH-Tests; sofort abgeschlossene
  Paare (`source_unreachable`-Bulk) zählen als `instant` für die
  Bar-Position, blähen die Rate aber nicht auf
- ETA aus der real-Rate berechnet
- Eigenes `bar_format` (tqdm-Rate entfernt, da sie mit `initial` falsch
  gerechnet hätte)

### Behoben
- `pairs_before_filter`-Snapshot für korrekte `total`/`initial`-Berechnung
  bei `--limit-pairs` in Kombination mit Resume/Retry

## [v0.2.0] (2026-08-19) — Retry-Feature

### Hinzugefügt
- Neues Flag `--retry-status S1,S2,…`: gezielt bestimmte Status neu testen
  (z. B. `source_unreachable,net_unreachable,tool_error`)
- Neues Flag `--retry-all-failed`: Shortcut, alle Paare außer `auth_ok`
  neu testen
- `prune_detail()`: entfernt betreffende Paare atomar aus `detail.csv`
  (Temp-Datei + Rename, abbruchsicher)
- Nur Paare, deren Endpunkte noch in der aktuellen IP-Liste stehen, werden
  retryt — Ergebnisse für entfernte IPs bleiben erhalten
- Kombinierbar mit neuen IPs: neue Paare + retry-Paare werden zusammen
  getestet

## [v0.1.0] (2026-08-19) — Initial Build

### Hinzugefügt
- SSH-Matrix-Tester: testet alle geordneten IP-Paare, ob Quelle A einen
  vollen SSH-Login auf Ziel B aufbauen kann (beide Richtungen, A→B und
  B→A sind je ein eigener Test)
- `paramiko` für Kali → A (persistente Verbindung pro Quelle, wird für
  alle Ziele der Quelle wiederverwendet)
- Fallback-Kette auf A für A → B:
  1. `sshpass` (falls auf A vorhanden)
  2. `ssh` + SSH_ASKPASS-Trick (askpass-Skript in `/tmp`/`/dev/shm`,
     Passwort nur in Umgebungsvariable)
  3. `nc` (nur Port-Check)
  4. `bash /dev/tcp` (nur Port-Check)
  5. Status `no_tool`
- Status-Klassifikation: `auth_ok`, `auth_fail`, `port_open`,
  `port_closed`, `net_unreachable`, `source_unreachable`, `no_tool`,
  `tool_error`, `unclear`
- Detail-CSV (`detail.csv`), inkrementell geschrieben (crash-sicher),
  `--resume` überspringt bereits getestete Paare
- 20 parallele Worker (Default), 1 Test pro Quell-IP (sanft),
  `--workers`/`--per-source` justierbar
- IP-Listen-Format: `<IP>`, `<IP>:<PORT>`, `<CIDR[:PORT]>`,
  `<Start>-<Ende[:PORT]>`, Labels, Kommentare
- `ssh_matrix_report.py`: erzeugt `matrix.csv` + `report.xlsx`
  - Sheet **Suche**: Dropdowns VON/NACH (Datenvalidierung mit versteckter
    Liste, umgeht 255-Zeichen-Limit), Ergebnis per `INDEX`/`MATCH`
  - Sheet **Matrix**: farbcodierte Pivot-Tabelle pro Host
  - Sheet **Detail**: alle Testzeilen mit AutoFilter + bedingter
    Formatierung
  - Sheet **Subnetze**: Statistik je /24
  - Sheet **Quelle**: Endpunkt-Mapping (IP, Port, Label, /24, /16)
  - Sheet **Liste**: Referenzliste der Endpunkte (Dropdown-Quelle)
- `--limit-pairs` für Trockenläufe
- Sicherheit: Passwort nur via Umgebungsvariable/Datei (nie als CLI-Arg),
  `StrictHostKeyChecking=no`, keine known_hosts-Verschmutzung,
  Temp-Dateien auf Zielen sofort gelöscht

[v2.0.1]: ./README.md
[v2.0.0]: ./README.md
[v1.2.6]: ./README.md
[v1.2.5]: ./README.md
[v1.2.4]: ./README.md
[v1.2.3]: ./README.md
[v1.2.2]: ./README.md
[v1.2.1]: ./README.md
[v1.2.0]: ./README.md
[v1.1.1]: ./README.md
[v1.1.0]: ./README.md
[v1.0.0]: ./README.md
[v0.5.0]: ./README.md
[v0.4.0]: ./README.md
[v0.3.0]: ./README.md
[v0.2.0]: ./README.md
[v0.1.0]: ./README.md
