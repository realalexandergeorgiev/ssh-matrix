# Entscheidungen

Technische Entscheidungen des SSH-Matrix-Testers mit Begründung. Chronologisch
nach Entwicklungsphasen (siehe auch `CHANGELOG.md`).

---

## 1. paramiko statt sshpass auf dem Kali

**Entscheidung:** Kali → A wird mit `paramiko` (Python) durchgeführt.

**Begründung:**
- Keine Installation von `sshpass` auf dem Kali nötig.
- Saubere parallele Sessions aus Python (Thread-Pool).
- Passwortübergabe direkt über die API (nicht in Prozess-Argumenten).
- `paramiko` ist auf Kali in der Regel bereits vorhanden
  (`python3-paramiko`).

**Verworfen:** `sshpass` auf dem Kali — funktioniert zwar, aber CLI-basiert,
Passwort sichtbar in `ps`, schwieriger zu parallelisieren und zu
fehlerbehandeln.

---

## 2. SSH_ASKPASS-Trick statt sshpass auf den Zielen

**Entscheidung:** Für den A → B-Hop wird kein `sshpass` auf A vorausgesetzt.
Falls es vorhanden ist, wird es genutzt; sonst greift der
SSH_ASKPASS-Trick.

**Begründung:**
- Auf den 220 Zielen kann nichts vorausgesetzt werden (verschiedene
  Systeme, minimale Installationen).
- SSH_ASKPASS ist ein Standard-OpenSSH-Mechanismus: `ssh` holt das
  Passwort über ein Skript, das es aus der Umgebungsvariable `__AP` liest.
- Das askpass-Skript wird nach `/tmp` (Fallback `/dev/shm`) geschrieben,
  Mode 700, und **sofort nach der Verbindung gelöscht**. Es enthält das
  Passwort nicht selbst.
- `SSH_ASKPASS_REQUIRE=force` (OpenSSH ≥ 8.4) erzwingt die Nutzung; bei
  älteren Versionen genügt `DISPLAY` + kein TTY (paramiko-exec hat kein TTY).

**Verworfen:** SSH-Key-Deploy auf alle Ziele — würde `authorized_keys`
dauerhaft verändern (verletzt NFR-2).

---

## 3. Fallback-Kette für den A → B-Hop

**Entscheidung:** Pro Ziel A wird zur Laufzeit erkannt, welche Werkzeuge
vorhanden sind (`sshpass`, `ssh`, `nc`, `bash`, `setsid`, OpenSSH-Version).
Der Test A → B nutzt den ersten verfügbaren Weg:

1. `sshpass` → voller SSH-Login mit Passwort als Argument.
2. `ssh` + `setsid` + SSH_ASKPASS → voller SSH-Login (Passwort in Env).
3. `nc -z -w T B <port>` → nur Port-Check (`port_open`/`port_closed`).
4. `bash -c 'exec 3<>/dev/tcp/B/<port>'` → nur Port-Check.
5. sonst → Status `no_tool`.

**Begründung:** Maximale Kompatibilität ohne Ziel-Modifikation. Der volle
Login (Rang 1/2) wird bevorzugt, Port-Checks (Rang 3/4) sind Abfallwege.
Die Fallback-Ebene pro Paar wird in `method` dokumentiert.

---

## 4. Persistente SSH-Verbindung pro Quelle

**Entscheidung:** Die Verbindung Kali → A wird **einmal** pro Quell-IP
aufgebaut und für alle ihre Ziele wiederverwendet (statt pro Test neu).

**Begründung:** Bei 220 Quellen × 219 Ziele würden sonst ~48.000
Neu-Verbindungen entstehen (Verbindungsaufbau, Auth-Handshake). Wiederverwendung
reduziert Last und Laufzeit erheblich.

**Konsequenz:** Ein Task pro Quelle; straggler-langsame Quellen dominieren
das Laufende (mehr `--per-source` verbessert die Lastverteilung).

---

## 5. Sanfte Parallelität (20 Worker, 1 Test pro Quelle)

**Entscheidung:** Default 20 parallele Quellen, 1 gleichzeitiger Test pro
Quell-IP.

**Begründung:** Schont die Ziele, vermeidet fail2ban/Rate-Limits. Beide Werte
sind justierbar (`--workers`, `--per-source`).

---

## 6. Beide Richtungen = alle geordneten Paare

**Entscheidung:** Es werden alle geordneten Paare (A, B) mit A ≠ B getestet;
A→B und B→A sind zwei eigene Tests.

**Begründung:** Einfach, lückenlos, keine Symmetrie-Annahme. Hin und Zurück
können sich real unterscheiden (Firewall-Regeln, asymmetrische Routing).

---

## 7. Inkrementelle CSV + `--resume` (Abbruchsicherheit)

**Entscheidung:** `detail.csv` wird zeilenweise geflusht; `--resume` liest
die bereits getesteten Paare und überspringt sie.

**Begründung:** Läufe dauern Stunden. Abbruch (Ctrl-C, Netz, Session-Reset)
darf keine Ergebnisse kosten. Der Retry-Prune (`prune_detail`) schreibt
atomar über Temp-Datei + `os.replace`.

---

## 8. Intelligentes Subnetz-Grouping (Gap-Clustering) statt festem /24

**Entscheidung:** Netze werden aus der IP-Verteilung geschätzt: IPs
sortieren, bei Lücken > `--subnet-gap` (Default 16) splitten, pro Cluster
das kleinste CIDR bilden. `--subnet-gap 0` = feste /24.

**Begründung:** Feste /24-Grenzen sind willkürlich — reale Netze können
kleiner (`/27`, `/30`) oder über /24-Grenzen hinweg gehen. Die
IP-Liste selbst ist die beste Informationsquelle über die tatsächliche
Netzstruktur.

**Verworfen:** Fester konfigurierbarer Prefix (`--subnet-prefix`) — nicht
„intelligent", müsste manuell angepasst werden. Adaptive Schwellwerte
(z. B. sqrt(Gruppengröße)) — schwerer vorhersehbar.

---

## 9. Paar-basierte Subnetz-Quota pro Richtung

**Entscheidung:** `--subnet-quota N` zählt pro Richtung (`src_net →
tgt_net`), wie viele distinct Quell-Hosts mind. 1 zählendes Ergebnis zu
irgendeinem Host im Ziel-Netz hatten. Was zählt, bestimmt `--quota-mode`:
`auth_ok` (nur voller SSH-Login, Default) oder `reachable`
(netzwerkseitig erreichbar: `auth_ok`, `auth_fail` oder `port_open`).
Sobald N erreicht ist, werden die restlichen Paare dieser Richtung als
`skipped` übersprungen. Die Zähler werden aus `detail.csv` geladen
(Resume-kompatibel) und gelten auch für unerreichbare Quellen.

**Begründung:**
- „Eine bestimmte Anzahl Hosts eines Netzes kann ein anderes Netz
  erreichen" ist die gewünschte Aussage — danach ist die Richtung als
  erreichbar bestätigt, weitere Tests wären redundant.
- Intra-Subnetz-Paare erreichen die Quota fast sofort → massiver
  Zeitgewinn, ohne dass jedes Host-Paar einzeln getestet wird.
- `skipped` ist ein eigener Status (kein Fehler), gezielt retrybar über
  `--retry-status skipped`.
- Der wählbare Modus trägt zwei Anwendungsfällen Rechnung: Strikte
  Prüfung (nur Login zählt) vs. reine Netzwerk-Aussage (Port
  erreichbar reicht). `auth_fail` zählt im `reachable`-Modus, weil es
  beweist, dass der TCP/SSH-Weg funktioniert — nur die Anmeldung
  scheitert.

**Verworfen:** Quota nur für Intra-Subnetz-Paare — die gleiche Logik ist
für Inter-Subnetz genauso nützlich.

---

## 10. Progress-Bar mit Resume-Offset und ehrlicher Rate

**Entscheidung:** Der Fortschrittsbalken startet bei `done/total` (Offset =
bereits getestete Paare aus Resume/Retry), zeigt eine eigene Rate `real/s`
nur aus echten SSH-Tests und zählt sofort abgeschlossene Paare
(`source_unreachable`-Bulk) separat als `instant`.

**Begründung:**
- Ohne Offset startete die Bar bei `0/remaining` statt `done/total` —
  fühlte sich an wie „falsch starten".
- `source_unreachable`-Bulk-Updates (bis 219 Paare auf einmal) blähten die
  tqdm-Rate auf („schnell und immer langsamer").
- tqdm's eingebaute Rate rechnet `n/elapsed` — mit `initial`-Offset wäre sie
  zusätzlich falsch. Deshalb eigenes `bar_format` ohne tqdm-Rate und eigene
  Berechnung (real/elapsed, ETA aus real).

---

## 11. Farbige Ausgabe (ANSI, nur bei TTY)

**Entscheidung:** Log-Zeilen und Live-Status-Zähler im Postfix werden
farbcodiert (auth_ok grün, auth_fail gelb, unreachable rot, …). Farben nur
wenn `sys.stderr.isatty()`; `NO_COLOR=1` deaktiviert; `run.log` bleibt
immer ungefärbt (eigener `ColorFormatter` nur für den stderr-Handler).

**Begründung:** Schnelle visuelle Bewertung während des Scans ohne den
Report öffnen zu müssen; keine Escape-Sequenzen in Dateien/Pipes.

---

## 12. Dropdowns via Datenvalidierung + versteckter Liste

**Entscheidung:** Die Dropdowns VON/NACH im Sheet „Suche" nutzen
Datenvalidierung mit Referenz auf das Sheet „Liste"
(`=Liste!$A$2:$A$N`) statt einer Inline-Liste.

**Begründung:** Excel-Inline-Listen sind auf 255 Zeichen begrenzt — mit
220+ IPs unmöglich. Die Referenz-Liste umgeht das Limit und liefert beim
Tippen Autovervollständigung („Vorschläge für VON/NACH").

---

## 13. Kein LICENSE (internes Tool)

**Entscheidung:** Keine Lizenzdatei. Das Projekt ist ein internes Tool und
nicht zur öffentlichen Veröffentlichung vorgesehen.

**Begründung:** Ohne Lizenz gilt standardmäßig „Alle Rechte vorbehalten" —
für interne Nutzung ausreichend. Falls doch veröffentlicht werden soll,
wäre eine Lizenz (z. B. MIT) nachzutragen.

---

## 14. Robustheit gegen hängende Worker & fehlerhafte Tool-Erkennung (v1.1.1)

**Entscheidung:**
- `exec_command` wird in einem Helper-Thread mit `join(timeout=30)`
  ausgeführt, statt direkt aufzurufen. Hintergrund: paramiko 2.12 blockiert
  in `exec_command` → `_wait_for_event()` → `event.wait()` **ohne Timeout** —
  ein nicht antwortender Server hätte den Worker (und damit einen
  Thread-Pool-Slot) dauerhaft blockiert.
- Der `KeyboardInterrupt`-Handler beendet per `os._exit(130)` nach dem
  Flushen von CSV/Progress. Hintergrund: `ThreadPoolExecutor.shutdown(wait=True)`
  und der Interpreter-Exit warten auf **alle** laufenden (ggf. hängenden)
  Nicht-Daemon-Worker-Threads — Ctrl+C hätte sonst ebenfalls gehängt.
- Nach dem Verbinden wird `transport.set_keepalive(10)` gesetzt, damit
  tote/halboffene Verbindungen erkannt werden.
- Die Tool-Erkennung wird bei leerer Probe **bis zu 3× wiederholt** und eine
  fehlgeschlagene Erkennung **nicht gecacht** (sonst wurden alle Ziele einer
  Quelle dauerhaft `NOTOOL`, wenn die Probe einmal transient fehlschlug).
  Zusätzlich WARNING mit Probe-Ausgabe ins `run.log`.
- Der askpass-Pfad verlangt **kein `setsid`** mehr (nur optional): paramiko-
  `exec_command` hat kein TTY, der SSH_ASKPASS-Trick funktioniert auch ohne;
  Minimal-Systeme mit `ssh` (dropbear) aber ohne `setsid`/`nc`/`bash` landeten
  sonst fälschlich bei `NOTOOL`. Ebenso wird das askpass-Skript per `printf`
  (POSIX) statt `base64 -d` angelegt — eine Abhängigkeit weniger auf den
  Zielen.

**Begründung:** Diese Kombination beseitigt zwei beobachtete Symptome:
(1) Lauf schien zu hängen, „verbunden, Tools:" erschien erst nach Ctrl+C
(hängende exec + wartender Executor-Shutdown); (2) häufiges `NOTOOL` trotz
vorhandener Werkzeuge (transiente Erkennungsfehler wurden permanent gecacht
bzw. `setsid`/`base64` wurden überzogen vorausgesetzt).

---

## 15. Pause-Menü zur Laufzeit (v1.2.0)

**Entscheidung:** 1× Ctrl+C öffnet ein interaktives Menü (Worker laufen
weiter), 2× Ctrl+C bricht hart ab (Exit 130). Menü-Befehle: `s` Stop,
`r` Zwischenbericht, `w N` Worker, `t N` Timeout, `q N` Subnetz-Quota,
`m M` Quota-Modus, `c` weiter. Der `ThreadPoolExecutor` wurde durch
Queue + daemon-Client-Threads ersetzt; veränderbare Parameter liegen in
`RunConfig`, `SourceTester.timeout` ist eine Property, die live aus der
Config liest. Ein `stop_event` wird im Worker-Loop vor jedem Test geprüft.

**Begründung:**
- `ThreadPoolExecutor.max_workers` ist nach der Erstellung **nicht änderbar**.
  Eine Worker-Anpassung zur Laufzeit (b) verlangt daher ein eigenes
  Thread-Pool-Modell (Queue + Consumer). Das gleiche Modell ermöglicht
  Stop (a), Parameter-Anpassung (c) und Zwischenbericht (d) sauber.
- Stop statt hartem Exit: laufende Tests werden nicht mitten in einer
  SSH-Verbindung abgebrochen; restliche Paare bleiben ungeschrieben und
  werden von `--resume` nachgeholt.
- Der Zwischenbericht ist bewusst **sprechend** (Klartext-Erklärung je
  Status) statt Kürzel, weil der Nutzer den Lauf ohne Nachschlagen
  bewerten können soll; Farben folgen den bestehenden Status-Farben.
- Ctrl+C als Trigger statt Keypress-Listener: keine Extra-Abhängigkeit,
  funktioniert über SSH, und „2× Ctrl+C = hart" ist ein vertrautes Muster.

---

## 16. RAM-Optimierung: Streaming statt O(n²) (v1.2.3)

**Entscheidung:** Die O(n²)-Strukturen wurden durch Streaming + Bitmaps
ersetzt:
- Keine `pairs`-Liste mehr — Tasks sind nur `(src_id, id_range)`, der
  Worker iteriert die Hosts direkt und filtert per Bitmap.
- `done`-Set (Resume) → **`PairBits`**-Bitmap (1 Bit/Paar).
- `--limit-pairs` und Retry-Paare ebenfalls als Bitmaps; `prune_detail`
  streamt (keine Zeilenliste mehr).
- `direction_working` wird auf `quota` Einträge pro Richtung gekappt.
- `estimate_ram_mb()` schätzt den Bedarf; über 1 GB (env-überschreibbar)
  Warnung + explizite `j/N`-Abfrage, `--force` überschreibt.

**Gemessen (3557 Endpunkte, 12,6 Mio. Paare, separater Prozess):**
vorher ~1000 MB, nachher ~34 MB Peak. Begründung: die Paarmenge ist
**n²** — jede materialisierte Struktur (Tupel-Liste, Set mit Strings,
Ziel-Listen) skaliert quadratisch und blieb den ganzen Lauf im RAM. Ein
Bitmap pro Paar ist nur 1 Bit (n²/8 Bytes) und damit selbst bei 50.000
Endpunkten unkritisch (~312 MB). Die Worker-Architektur (Queue +
Consumer, v1.2.0) blieb unverändert — nur die Task-Inhalte und die
Resume/Retry-Datenhaltung wurden umgestellt.

**Abgewogen:** `done` als Set von Int-Tupeln (~60 B/Paar) wäre einfacher
gewesen, bleibt aber O(n²) (bei 12,6 Mio. Paaren ~760 MB). Das Bitmap ist
~500× kleiner bei gleicher O(1)-Abfrage.

---

## 17. TUI (Textual) statt tqdm-Bar + Pause-Menü (v2.0.0)

**Entscheidung:** v2.0 bringt eine **Textual-TUI** (Panes: Status, Settings,
Graphen, Log; Modal-Bestätigung für Stop/Quit) mit Auto-Detect (TTY +
textual installiert → TUI; `--tui`/`--no-tui` zum Erzwingen). Der CLI-Modus
wurde vereinfacht: tqdm und das Ctrl+C-Menü sind entfernt, stattdessen
Log-Zeilen + periodischer Status-Einzeiler (`--status-interval`); 1× Ctrl+C
= sauberer Stop (resume-fähig), 2× = hart. `Progress` wurde zu `RunStats`
(thread-sichere Statistik mit Historie für Graphen).

**Begründung:**
- Die tqdm-Bar wurde durch Menü-Output zerstört und war bei riesigen Totals
  (0 %-Anzeige) kaum lesbar. Eine echte TUI mit Panes löst beides sauber:
  die Log-Zeilen wandern ins Log-Pane, die Status-Zeile ins Status-Pane,
  und Stop/Quit laufen über ein Bestätigungs-Modal (Anforderung
  „Warnung beim Abbrechen").
- **Textual statt Rich-Live/curses**: btop-artige Optik (Sparklines,
  Panes, Modals, Key-Bindings) mit wenig Eigenbau. Apt-Version (0.1.13)
  ist veraltet → pip-Installation; CLI bleibt davon unabhängig (lazy
  Import, tqdm entfällt als Abhängigkeit).
- **Auto-Detect statt Flag-Pflicht**: interaktive Nutzung bekommt die TUI
  automatisch; Pipes/Automation laufen unverändert im CLI-Modus.
- **CLI vereinfacht statt beibehalten**: zwei parallele Interaktionspfade
  (Menü + TUI) wären Wartungslast; der Einzeiler + sauberer Ctrl+C-Stop
  deckt Automation und schnelle Läufe ab.
- **Worker-Architektur unverändert**: Queue + Consumer (v1.2.0) und die
  Bitmaps (v1.2.3) bleiben; die TUI ist nur eine neue Anzeige-Ebene über
  `RunStats`.

**Erkanntes Problem beim Umbau:** `threading.Lock` ist nicht reentrant —
`maybe_print_status` hielt den Lock und rief `status_line()` auf, das ihn
erneut erwarb (Deadlock). Gelöst über `_status_line_locked()` (Lock muss
schon gehalten sein).

---

## 18. Pause-and-Retry bei Auth-Fail-Block (v2.0.2)

**Entscheidung:** `--auth-pause` (z.B. `5m`, `300`) pausiert global, wenn
`auth_fail`-Häufung einen Block signalisiert (Schwelle/Fenster:
`--auth-pause-threshold`/`--auth-pause-window`), und retryt das
getroffene Paar. Gilt für Kali→A (connect) und A→B (test_target) gleichermaßen.
`tqdm`-Warnhinweis + TUI-Log, interruptible Sleep, danach Retry. Live via
TUI `a`.

**Begründung:** Fail2Ban/Rate-Limit löst nach wenigen `Auth fail` eine
temporäre Sperre aus — alle folgenden Logins schlagen falsch-positiv fehl.
Re-Retry ohne Pause verschlimmert den Block; eine globale Pause entlastet
den Server und vermeidet Fehlklassifikation. Die bestehenden Phasen
(Keyboard-Interactive-Fallback, `PreferredAuthentications=password,keyboard-interactive`)
helfen nicht gegen Block — nur Warten hilft.
