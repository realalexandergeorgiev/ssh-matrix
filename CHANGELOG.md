# Changelog

Alle nennenswerten Änderungen am SSH-Matrix-Tester.

Das Format folgt [Keep a Changelog](https://keepachangelog.com/de/1.1.0/).
Dieses Projekt verwendet [Semantic Versioning](https://semver.org/lang/de/).

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

[v1.0.0]: ./README.md
[v0.5.0]: ./README.md
[v0.4.0]: ./README.md
[v0.3.0]: ./README.md
[v0.2.0]: ./README.md
[v0.1.0]: ./README.md
