# SSH-Matrix-Tester

**Version v1.2.1** — entwickelt von **Alex & DeepSeek**.

Testet für alle geordneten IP-Paare, ob **Quelle A einen vollen SSH-Login auf
Ziel B aufbauen kann** – mit denselben Credentials (User/Passwort) auf allen
Hosts. Es werden **beide Richtungen** getestet (A→B und B→A sind je ein
eigener Test).

## Features

- **Voller SSH-Login-Test** (nicht nur Port-Check) für alle Paare, beide
  Richtungen — ohne sshpass auf den Zielen (SSH_ASKPASS-Fallback)
- **Abbruchsicher**: inkrementelle `detail.csv` + `--resume` + gezielter
  Re-Test (`--retry-status`, `--retry-all-failed`)
- **Schnell**: 20 parallele Worker, persistente Verbindung pro Quelle,
  `--subnet-quota` überspringt redundante Paare sobald eine Richtung bestätigt ist
- **Intelligentes Subnetz-Grouping** (Gap-Clustering statt starrem /24)
- **Excel-Report** (`report.xlsx`): Dropdown-Suche VON → NACH mit
  Vorschlägen, Host- und **Netz-Matrix**, Detail mit AutoFilter,
  Subnetz-Statistik — dazu `matrix.csv`, `netz_matrix.csv`, `detail.csv`
- **Farbige Terminal-Ausgabe** mit Live-Status-Zählern
- **Sicher**: Passwort nur via Umgebungsvariable/Datei, keine
  Ziel-Modifikation, keine known_hosts-Verschmutzung

---

## Inhaltsverzeichnis

- [1. Anforderungen (Remote-Kali)](#1-anforderungen-remote-kali)
- [2. Schnellstart](#2-schnellstart)
- [3. IP-Listen-Format (`ips.txt`)](#3-ip-listen-format-ipstxt)
- [4. CLI-Referenz](#4-cli-referenz)
- [5. Ablauf & Architektur](#5-ablauf--architektur)
- [6. Output](#6-output)
- [7. Entscheidungen & Begründung](#7-entscheidungen--begründung)
- [8. Sicherheit](#8-sicherheit)
- [9. Laufzeit & Ressourcen](#9-laufzeit--ressourcen)
- [10. Troubleshooting](#10-troubleshooting)
- [11. Annahmen & Einschränkungen](#11-annahmen--einschränkungen)

Weitere Dokumente: [ANFORDERUNGEN.md](ANFORDERUNGEN.md),
[ENTSCHEIDUNGEN.md](ENTSCHEIDUNGEN.md), [CHANGELOG.md](CHANGELOG.md).

---

## 1. Anforderungen (Remote-Kali)

Das Skript läuft auf deiner **Kali-Maschine**. Dort einmalig:

```bash
sudo apt update
sudo apt install -y python3-paramiko python3-openpyxl python3-tqdm
python3 -c "import paramiko, openpyxl, tqdm; print('OK')"
```

Alternative per pip (falls apt-Versionen zu alt sind):

```bash
sudo apt install -y python3-pip
pip3 install --break-system-packages paramiko openpyxl tqdm
```

**Nicht nötig:** `sshpass` und GNU `parallel` auf dem Kali (paramiko übernimmt
Kali→A). Auf den **Zielhosts** werden `sshpass`, `ssh`, `nc`, `bash` zur
Laufzeit automatisch erkannt und je nach Verfügbarkeit eine Fallback-Kette
verwendet.

Kopieren aufs Kali, z. B.:

```bash
scp -r ssh-matrix/ user@kali:~
```

---

## 2. Schnellstart

```bash
cd ssh-matrix

# 1. IP-Liste anlegen (Vorlage: ips.txt.example)
cp ips.txt.example ips.txt
#    ... ips.txt anpassen ...

# 2. Passwort NICHT als Argument setzen, sondern als Umgebungsvariable:
export SSHPASS='deinPasswort'

# 3. Testlauf starten (alle Paare, beide Richtungen)
python3 ssh_matrix.py --ips ips.txt --user meinuser --pass-env SSHPASS --out ssh_matrix_out

# 4. Report erzeugen (Matrix-CSV + report.xlsx)
python3 ssh_matrix_report.py --detail ssh_matrix_out/detail.csv --out ssh_matrix_out
```

Nach Abbruch (Ctrl-C, Stromausfall, Session-Reset) einfach mit `--resume`
weiterlaufen lassen – bereits getestete Paare werden übersprungen:

```bash
python3 ssh_matrix.py --ips ips.txt --user meinuser --pass-env SSHPASS --out ssh_matrix_out --resume
```

Der Fortschrittsbalken zeigt bei `--resume` den **Gesamtfortschritt** über alle
Läufe hinweg (z. B. startet er bei `18000/48000`, wenn 18.000 Paare schon
getestet sind). Die angezeigte Rate (`real/s`) zählt nur echte SSH-Tests –
sofort markierte Paare (z. B. `source_unreachable`, wenn eine Quelle vom Kali
aus tot ist) erscheinen separat als `instant` und blähen die Rate nicht auf.

Die Ausgabe ist **farbcodiert** (sofern stderr ein Terminal ist; `NO_COLOR=1`
deaktiviert Farben, `run.log` ist immer ungefärbt): `auth_ok` grün, `auth_fail`
gelb, Timeouts/Unreachable rot usw. – sowohl in den Log-Zeilen als auch als
Live-Zähler im Fortschrittsbalken (z. B. `OK:123 AUTH:5 UNREACH:3`).

### Pause-Menü zur Laufzeit (1× Ctrl+C)

Während des Laufs öffnet **1× Ctrl+C** ein interaktives Menü (die Worker
laufen weiter). Im Menü selbst zeigt **1× Ctrl+C** nur einen Hinweis und
lässt das Menü offen — erst **2× Ctrl+C hintereinander** bricht hart ab
(Exit 130):

```
=== Pause-Menue ===
  s     Stop (sauber herunterfahren, Resume-faehig)
  r     Zwischenbericht
  w N   Worker auf N setzen (aktuell 2)
  t N   Timeout auf N Sekunden (aktuell 10)
  q N   Subnetz-Quota auf N (aktuell 3)
  m M   Quota-Modus auth_ok|reachable (aktuell auth_ok)
  v L   Verbosity err|warn|info (aktuell info)
  c     Weiter
  ctrl-c  1x Hinweis / 2x hintereinander = hart abbrechen (Exit 130)
menu>
```

- **`s`** stoppt sauber: Worker beenden den aktuellen Test, die restlichen
  Paare bleiben ungeschrieben → `--resume` testet sie beim nächsten Lauf.
- **`r`** zeigt einen sprechenden, farbigen Zwischenbericht: Fortschritt +
  ETA, Status-Verteilung mit Klartext-Erklärung (z. B. „Login abgelehnt –
  SSH-Port offen, aber Passwort/User falsch") und Subnetz-Erreichbarkeit
  (bestätigte Richtungen vs. Richtungen mit Lücken).
- **`w N` / `t N` / `q N` / `m M` / `v L`** ändern Parameter zur Laufzeit; die
  Wirkung greift für noch nicht getestete Paare (`v L` = Verbosity der
  Terminal-Ausgabe, `err|warn|info`). `w 0` pausiert die Arbeit
  komplett (alle Worker beenden sich nach dem aktuellen Test), `w N` mit
  N>0 nimmt sie wieder auf.

### Neue IPs hinzufügen (inkrementell)

Neue IPs einfach in `ips.txt` eintragen (alte bleiben drin) und mit `--resume`
im selben Ordner laufen lassen. Es werden **nur Paare mit mind. einer neuen IP**
getestet (neu→alt, alt→neu, neu→neu); alt→alt ist schon in `detail.csv` und wird
übersprungen. Danach den Report neu bauen:

```bash
python3 ssh_matrix.py --ips ips.txt --user meinuser --pass-env SSHPASS --out ssh_matrix_out --resume
python3 ssh_matrix_report.py --detail ssh_matrix_out/detail.csv --out ssh_matrix_out
```

### Alte Fehlversuche gezielt neu testen

`--resume` überspringt **alle** bereits getesteten Paare – auch die, die damals
fehlgeschlagen sind (z. B. `source_unreachable` bei kurzem Netz-Ausfall). Um
solche gezielt neu zu testen, gibt es zwei Flags (beide implizieren `--resume`):

```bash
# Nur bestimmte Status neu testen:
python3 ssh_matrix.py --ips ips.txt --user meinuser --pass-env SSHPASS --out ssh_matrix_out \
    --retry-status source_unreachable,net_unreachable,tool_error

# Oder: alles außer auth_ok neu testen:
python3 ssh_matrix.py --ips ips.txt --user meinuser --pass-env SSHPASS --out ssh_matrix_out --retry-all-failed
```

**Wirkungsweise:** Die betreffenden Paare werden aus `detail.csv` entfernt
(atomar, kein Datenverlust bei Abbruch) und dann neu getestet. Paare von IPs,
die nicht mehr in `ips.txt` stehen, bleiben unangetastet. Kombinierbar mit
neuen IPs (s. o.): neue Paare werden ohnehin getestet, alte Fehlversuche per
`--retry-status` zusätzlich. Danach wie immer den Report neu bauen.

---

## 3. IP-Listen-Format (`ips.txt`)

Eine Zeile = ein Eintrag. Unterstützt:

| Format | Beispiel | Bedeutung |
|---|---|---|
| `<IP>` | `10.0.0.5` | Port 22 (Default) |
| `<IP>:<PORT>` | `10.0.0.5:2222` | abweichender SSH-Port |
| `<IP>/<CIDR>` | `10.0.1.0/24` | Subnetz expandieren (Port 22) |
| `<IP>/<CIDR>:<PORT>` | `10.0.2.0/24:2200` | Subnetz mit Port |
| `<IP1>-<IP2>` | `10.10.0.10-10.10.0.20` | IP-Range |
| `<IP1>-<IP2>:<PORT>` | `10.10.0.30-10.10.0.40:2222` | Range mit Port |
| Label | `10.0.0.13 # webserver1` | Kommentar/Label am Zeilenende |

- Kommentarzeilen beginnen mit `#`, Leerzeilen werden ignoriert.
- Duplikate (gleiche IP + Port) werden entfernt.
- Nur IPv4.

Der **Port eines Eintrags gilt für beide Richtungen**: Kali verbindet sich zu
A auf A's Port; von A aus wird B auf B's Port getestet (`ssh -p <B_port>`).

---

## 4. CLI-Referenz

### `ssh_matrix.py`

| Argument | Default | Bedeutung |
|---|---|---|
| `--ips FILE` | – (Pflicht) | IP-Liste (siehe Abschnitt 3) |
| `--user USER` | – (Pflicht) | SSH-User, gilt für alle IPs |
| `--pass-env NAME` | `SSHPASS` | Env-Variable mit Passwort (empfohlen) |
| `--pass-file FILE` | – | Datei, 1. Zeile = Passwort (Alternative) |
| `--verbose LEVEL` | `info` | Detailgrad der Terminal-Ausgabe: `err` (nur Fehler), `warn` (Fehler + Warnungen), `info` (alles). `run.log` bleibt immer vollständig — nützlich, wenn Log-Zeilen das Pause-Menü stören. |
| `--force` | aus | RAM-Warnung (Schätzung über Schwelle, Default 1 GB) ohne Nachfrage überschreiben |
| `--port-default N` | `22` | Default-Port für Einträge ohne `:PORT` |
| `--workers N` | `20` | Parallele Quell-IPs |
| `--timeout N` | `10` | Timeout je Hop (Connect/Auth/Befehl) in s |
| `--per-source N` | `1` | Max. gleichzeitige Tests pro Quell-IP |
| `--out DIR` | `ssh_matrix_out` | Ausgabe-Verzeichnis |
| `--resume` | aus | Bereits getestete Paare überspringen |
| `--retry-status S1,S2,…` | – | Status, die neu getestet werden (impliziert `--resume`). Z. B. `source_unreachable,skipped`. Gültig: `auth_ok auth_fail port_open port_closed net_unreachable source_unreachable no_tool tool_error unclear skipped` |
| `--retry-all-failed` | aus | Shortcut: alle Paare außer `auth_ok`/`skipped` neu testen (impliziert `--resume`) |
| `--subnet-quota N` | 0 (aus) | Mindestanzahl erfolgreicher Quell-Hosts pro Richtung (`src_net → tgt_net`); danach Rest als `skipped` überspringen |
| `--quota-mode MODE` | `auth_ok` | Was zählt als „funktionierender Quell-Host" für `--subnet-quota`: `auth_ok` (nur voller Login) oder `reachable` (netzwerkseitig erreichbar: `auth_ok`, `auth_fail` oder `port_open`) |
| `--subnet-gap N` | 16 | Lücken-Schwellwert für Subnetz-Clustering (0 = feste /24) |
| `--limit-pairs N` | 0 (alle) | Nur die ersten N Paare testen (Trockenlauf) |

### `ssh_matrix_report.py`

| Argument | Default | Bedeutung |
|---|---|---|
| `--detail FILE` | – (Pflicht) | `detail.csv` aus dem Testlauf |
| `--out DIR` | `ssh_matrix_out` | Ausgabe-Verzeichnis |
| `--matrix-name NAME` | `matrix.csv` | Name der Matrix-CSV |
| `--xlsx-name NAME` | `report.xlsx` | Name der Excel-Datei |

---

## 5. Ablauf & Architektur

**Test pro geordnetem Paar (A→B):**

1. **Kali → A** per paramiko (User/Passwort). Die Verbindung wird pro Quell-IP
   **einmal aufgebaut und für alle ihre Ziele wiederverwendet** (großer
   Performance-Gewinn, keine 48.000 Neu-Verbindungen).
2. **Tool-Erkennung auf A** (einmalig, gecacht): `sshpass`, `ssh`, `nc`,
   `bash`, `setsid`, OpenSSH-Version.
3. **Test A → B** mit Fallback-Kette:

   | Rang | Voraussetzung auf A | Test |
   |---|---|---|
   | 1 | `sshpass` | `sshpass -p PASS ssh -p <B_port> … user@B 'echo CONN_OK_…'` |
   | 2 | `ssh` + `setsid` | `ssh` mit **SSH_ASKPASS-Trick** (siehe unten) |
   | 3 | `nc` | `nc -z -w T B <B_port>` → nur Port-Check |
   | 4 | `bash` | `timeout T bash -c 'exec 3<>/dev/tcp/B/<B_port>'` → nur Port-Check |
   | 5 | – | Status `no_tool` |

4. **Status-Klassifizierung** (aus Ausgabe/Exit-Code):

   | Status | Bedeutung |
   |---|---|
   | `auth_ok` | Voller SSH-Login A→B erfolgreich |
   | `auth_fail` | SSH-Port offen, Login abgelehnt |
   | `port_open` | Nur Port erreichbar (Fallback 3/4) |
   | `port_closed` | Port zu (`Connection refused`) |
   | `net_unreachable` | Timeout / No Route / Network unreachable |
   | `source_unreachable` | A war vom Kali aus nicht erreichbar → alle A→B übersprungen |
   | `no_tool` | Auf A keinerlei Testwerkzeug |
   | `tool_error` | Unerwarteter Fehler (Fehlertext in `error`-Spalte) |
   | `unclear` | Nicht eindeutig (nur Fallback-Port-Check) |
   | `skipped` | Übersprungen (Subnetz-Quota für diese Richtung erreicht) |

**Beide Richtungen** entstehen automatisch, weil alle geordneten Paare
getestet werden. `direction` in `detail.csv` ist daher immer `forward` –
Hin und Zurück sind zwei Zeilen mit vertauschten `source`/`target`.

### Subnetz-Quota (schneller testen)

Mit `--subnet-quota N` wird pro Richtung (`src_net → tgt_net`) nach N
Quell-Hosts der Rest als `skipped` übersprungen. Welche Ergebnisse als
„funktionierender Quell-Host" zählen, bestimmt `--quota-mode`:

- `auth_ok` (Default): nur voller SSH-Login zählt.
- `reachable`: auch netzwerkseitig erreichbare Hosts zählen
  (`auth_ok`, `auth_fail` oder `port_open`) — beweist, dass der Weg zum
  Ziel-Netz funktioniert, auch wenn die Anmeldung fehlschlägt.

```bash
# Nur voller Login zählt:
python3 ssh_matrix.py --ips ips.txt --user USER --pass-env SSHPASS --subnet-quota 3

# Netzwerk-Erreichbarkeit reicht:
python3 ssh_matrix.py --ips ips.txt --user USER --pass-env SSHPASS --subnet-quota 3 --quota-mode reachable
```

Die Netze werden nicht starr als /24 angenommen, sondern anhand der
IP-Verteilung intelligent geschätzt (Gap-Clustering, `--subnet-gap`, Default 16).
`--subnet-gap 0` schaltet auf feste /24 um.

Übersprungene Paare haben Status `skipped` und können gezielt mit
`--retry-status skipped` neu getestet werden (z. B. wenn die Quota erhöht wird).
`--retry-all-failed` retryt `skipped` **nicht** (kein Fehler, nur nicht getestet).

---

## 6. Output

### `ssh_matrix_out/detail.csv`

```
timestamp,source_ip,source_port,source_label,src_24,src_16,target_ip,target_port,
target_label,tgt_24,tgt_16,direction,method,status,latency_ms,error
```

Wird **inkrementell** geschrieben (jede Zeile sofort geflusht) – bei Abbruch
bleibt alles Vorherige erhalten (`--resume`).

### `ssh_matrix_out/matrix.csv`

Pivot: Zeilen = Quelle, Spalten = Ziel, Zelle = Kurzcode
(`OK`, `AUTH`, `PORT`, `CLOSED`, `UNREACH`, `SRCERR`, `NOTOOL`, `ERR`, `SKIP`, `?`).

### `ssh_matrix_out/netz_matrix.csv`

Subnetz-Pivot: Zeilen = Quell-Netz, Spalten = Ziel-Netz, Zelle = `ok/tested`
(z. B. `3/5` = 3 von 5 getesteten Paaren erfolgreich; `–` = alles skipped/nicht getestet).

### `ssh_matrix_out/report.xlsx` (Sheets)

| Sheet | Inhalt |
|---|---|
| **Suche** | Dropdowns **VON** und **NACH** (Datenvalidierung). Beim Tippen schlägt Excel passende Einträge vor. Ergebnis erscheint per `INDEX/MATCH` aus der Matrix. Legende rechts daneben. |
| **Matrix** | Farbcodierte Pivot-Tabelle pro Host (grün = Login ok, rot = unreachable …) |
| **Netz-Matrix** | Farbcodierte Pivot-Tabelle pro Subnetz: `ok/tested` pro Richtung. Grün = auth_ok vorhanden, gelb = nur Port, rot = alle failed, grau = nicht getestet/skipped. |
| **Detail** | Alle Testzeilen mit AutoFilter (Filtern nach Quelle/Target) + bedingter Formatierung |
| **Subnetze** | Statistik je /24: Endpunkte, Quellen mit Login-Erfolg, als Ziel erreichbar, skipped (Quota) |
| **Quelle** | Endpunkt-Mapping: IP, Port, Label, /24, /16, Netz |
| **Liste** | Referenzliste der Endpunkte (Datenquelle der Dropdowns – so bleiben die Dropdowns auch bei 220+ IPs gültig) |

---

## 7. Entscheidungen & Begründung

| Entscheidung | Begründung |
|---|---|
| **paramiko statt sshpass auf Kali** | Keine Installation nötig, robuste parallele Sessions, saubere Passwortübergabe. |
| **Fallback-Kette statt Pflicht-`sshpass` auf Zielen** | Auf den 220 Zielen kann nichts vorausgesetzt werden. `sshpass` wird nur genutzt, wenn vorhanden. |
| **SSH_ASKPASS-Trick** | Ermöglicht echten Passwort-Login A→B ohne `sshpass` auf A und ohne Ziel-Modifikation. Das askpass-Skript wird nach `/tmp` (Fallback `/dev/shm`, falls `/tmp` noexec/ro) geschrieben, liest das Passwort aus der **Umgebungsvariable** `__AP` (enthält es also nicht selbst), hat Mode 700 und wird nach der Verbindung sofort gelöscht. |
| **Kein SSH-Key-Deploy** | Deployen eines Keys auf alle Ziele wurde bewusst verworfen: verändert `authorized_keys` auf 220 Systemen. |
| **Persistente Verbindung pro Quelle** | Kali→A wird nicht je Test neu aufgebaut, sondern wiederverwendet → deutlich weniger Last und Zeit. |
| **Sanfte Parallelität (Default: 20 Worker, 1 Test pro Quell-IP)** | Schont die Ziele, vermeidet fail2ban/Rate-Limit-Probleme. `--workers`/`--per-source` sind justierbar. |
| **Beide Richtungen = alle geordneten Paare** | Einfach, lückenlos; Hin/Zurück sind zwei eigene Zeilen. |
| **Inkrementelle CSV + `--resume`** | Lange Laufzeit (s. u.) abbruchsicher. |
| **Dropdowns via Datenvalidierung + versteckter Liste** | Excel-Limits: eine Inline-Liste ist auf 255 Zeichen begrenzt – mit Referenz auf das Sheet `Liste` funktionieren 220+ Einträge. |
| **Host-Keys ignoriert** | `StrictHostKeyChecking=no` + leere `UserKnownHostsFile` → keine Prompts, keine known_hosts-Verschmutzung. Host-Key-Änderungen werden bewusst ignoriert. |

---

## 8. Sicherheit

- **Passwort nie als CLI-Argument** (sonst in `ps` sichtbar). Entweder
  Umgebungsvariable (`--pass-env`) oder Datei (`--pass-file`, 1. Zeile).
- Auf A: Passwort steht nur in der **Umgebungsvariable** des ssh-Prozesses –
  nicht in der /tmp-Datei, nicht in Prozess-Argumenten.
- Einzige Ausnahme: Die `sshpass`-Variante auf A übergibt das Passwort als
  Argument (`-p`) – dann ist es auf A kurzzeitig in `ps` sichtbar. Wenn das
  nicht akzeptabel ist, `sshpass` auf den Zielen deinstallieren → es greift
  automatisch der askpass-Weg.
- Temp-Dateien auf A werden im `finally`-Cleanup entfernt; SSH-Verbindungen
  werden geschlossen.
- Der Test führt auf B **nur** `echo CONN_OK_…` aus – keine Systemänderung.

---

## 9. Laufzeit & Ressourcen

Bei 220 IPs: **≈ 48.000 Einzeltests** (220 × 219). Bei Default-Einstellungen
(20 Worker, 1 pro Quelle, 10 s Timeout) realistisch **ca. 2,5–3,5 h**.
Tendenzen:

- Schneller: `--workers 50 --per-source 2 --timeout 6` (mehr Last, ggf.
  fail2ban-Risiko auf den Zielen).
- Schonender: `--workers 10 --timeout 15`.

Mit `--limit-pairs 50` lässt sich der Ablauf vorab an ein paar Paaren
verifizieren (Trockenlauf).

### RAM-Verbrauch (seit v1.2.3 stark reduziert)

Das Skript arbeitet **streaming** statt O(n²)-Strukturen im Speicher zu
halten: keine `pairs`-Liste, `done`-Set als **Bitmap** (1 Bit/Paar),
Retry/`--limit-pairs` ebenfalls als Bitmaps. Gemessener Spitzenverbrauch
(separater Prozess):

| Endpunkte | Paare | vor v1.2.3 | ab v1.2.3 |
|---|---|---|---|
| 220 | 48.180 | ~39 MB | ~35 MB |
| 1.000 | 999.000 | ~107 MB | ~36 MB |
| 3.557 | 12,6 Mio. | **~1000 MB** | **~34 MB** |

Vor dem Start schätzt das Skript den Bedarf; über der Schwelle (Default
**1 GB**, per `SSH_MATRIX_RAM_WARN_MB` anpassbar) erscheint eine Warnung
mit expliziter Nachfrage **„Trotzdem fortfahren? [j/N]"** (ohne Terminal
wird abgebrochen; `--force` überschreibt die Nachfrage). Die
Schätzung bleibt bewusst konservativ, da die Streaming-Architektur den
Verbrauch praktisch linear hält.

---

## 10. Troubleshooting

| Symptom | Ursache / Lösung |
|---|---|
| `source_unreachable` für alle Ziele einer Quelle | Kali → A fehlgeschlagen: IP/PORT/User/Passwort prüfen. A war vom Kali aus nicht erreichbar. |
| `no_tool` | Auf A wurde kein `sshpass`/`ssh`/`nc`/`bash` erkannt. **Seit v1.1.1** schreibt das Skript in `run.log` eine WARNING mit der gekürzten Probe-Ausgabe → dort nachsehen. Wenn die Probe leer ist (z. B. `channel closed`), war es ein transientes Erkennungsproblem und kein echtes `no_tool` — der nächste Lauf (`--resume`) versucht es erneut. |
| Lauf „hängt" / „Tools" erscheint erst nach Ctrl+C | Seit v1.1.1 behoben: `exec_command` ist auf 30 s begrenzt, tote Verbindungen werden per Keepalive erkannt, und Ctrl+C beendet sofort (Exit 130, Ergebnisse bleiben erhalten). |
| `auth_fail` | Port offen, aber Login mit dem übergebenen Passwort abgelehnt. |
| `unclear` | Nur beim Port-Fallback (Rang 3/4): nicht unterscheidbar, ob Port zu oder gefiltert. |
| `ERR` mit leerer `error`-Spalte | Verbindung zu A tot – Skript reconnectet automatisch; andernfalls `--resume` nach Laufende. |
| Viele `UNREACH` | Firewalls zwischen den Netzen – ggf. relevantes Ergebnis selbst. |
| fail2ban/Rate-Limit | Lauf mit weniger `--workers`, `--per-source 1`, größerem `--timeout`; am Ende `--resume`. |
| `run.log` | Detailliertes Laufprotokoll in `ssh_matrix_out/run.log` (Tool-Erkennung, Reconnects, Warnungen). |

**Status-Bedeutung (OK / AUTH / NOTOOL):** `OK` = voller SSH-Login A→B
erfolgreich. `AUTH` = SSH-Port offen, Login abgelehnt. `NOTOOL` = **kein Test
durchgeführt** (kein Werkzeug auf Quelle A erkannt — kein Ergebnis über B!).
Der `OK`-Zähler im Fortschrittsbalken zählt nur A→B-Paare **dieses Laufs**;
die grünen „Quelle verbunden"-Zeilen (Kali→A) zählen nicht. Bei
`--subnet-quota` werden nach erreichter Quota weitere Paare als `SKIP`
übersprungen — OK bleibt dann bewusst klein.

---

## 11. Annahmen & Einschränkungen

- **IPv4** only (keine IPv6-Subnetze/Ranges).
- **Gleiche Credentials** auf allen IPs (User + Passwort).
- SSH-Default-Port 22, abweichende Ports nur via `IP:PORT` in der Liste.
- Der Testlauf startet auf dem Kali; das Kali selbst wird nicht getestet
  (nur Quelle=Ziel=Kali-Paare wären nötig – nicht vorgesehen).
- `/tmp` (oder `/dev/shm`) auf den Zielen muss beschreibbar und ausführbar
  sein, damit der askpass-Fallback (Rang 2) greifen kann.
- Host-Key-Änderungen werden ignoriert (Absicht, siehe Abschnitt 7).
- Port-Checks (Rang 3/4) prüfen nur die Erreichbarkeit von Port 22, nicht
  den Login – der volle Login braucht Rang 1/2.
