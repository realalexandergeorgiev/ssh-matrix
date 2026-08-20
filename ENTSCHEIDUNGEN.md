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
