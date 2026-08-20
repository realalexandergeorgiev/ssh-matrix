# Anforderungen

Anforderungen an den SSH-Matrix-Tester. Formatiert als funktionale und
nicht-funktionale Anforderungen mit Prioritäten.

**Legende Prioritäten:** M = Muss (Pflicht), K = Kann (optional, nice-to-have),
S = Soll (wünschenswert, aber verzichtbar).

---

## 1. Kontext & Ziel

Der SSH-Matrix-Tester prüft für eine Liste von per SSH erreichbaren IPs, ob
jede IP zu jeder anderen IP einen vollen SSH-Login aufbauen kann. Ziel ist ein
Erreichbarkeits- und Vertrauensmodell der Umgebung auf Host- und
Subnetz-Ebene. Eingesetzt von einer Kali-Maschine aus; Ergebnisse werden als
CSV und Excel-Report aufbereitet.

---

## 2. Funktionale Anforderungen

| ID | Priorität | Anforderung |
|---|---|---|
| FR-1 | M | Für jedes geordnete Paar (A, B) mit A ≠ B wird getestet, ob A per SSH-Login B erreichen kann. **Beide Richtungen** werden getestet (A→B und B→A sind zwei eigene Tests). |
| FR-2 | M | Der Test ist ein **voller SSH-Login** (nicht nur Port-Check): Quelle A meldet sich mit User/Passwort auf Ziel B an und führt `echo CONN_OK_…` aus. |
| FR-3 | M | User und Passwort gelten **einheitlich für alle IPs** und werden dem Skript einmalig übergeben (Umgebungsvariable oder Datei, nie als CLI-Argument). |
| FR-4 | M | Kali → A wird per `paramiko` durchgeführt. A → B nutzt eine **Fallback-Kette** auf A: `sshpass` → SSH_ASKPASS-Trick (`ssh`+`setsid`) → `nc` → `bash /dev/tcp` → Status `no_tool`. |
| FR-5 | M | Das Ergebnis pro Paar wird als Zeile in `detail.csv` geschrieben: timestamp, source/target (IP, Port, Label, /24, /16, Netz), direction, method, status, latency_ms, error. |
| FR-6 | M | `detail.csv` wird **inkrementell** geschrieben (jede Zeile sofort geflusht) — bei Abbruch bleibt alles Vorherige erhalten. |
| FR-7 | M | `--resume` überspringt bereits getestete Paare (aus `detail.csv`). |
| FR-8 | M | Paralleler Test mit konfigurierbarer Worker-Zahl (`--workers`, Default 20) und max. gleichzeitiger Tests pro Quell-IP (`--per-source`, Default 1). |
| FR-9 | M | IP-Liste unterstützt `<IP>`, `<IP>:<PORT>`, `<CIDR[:PORT]>`, `<Start>-<Ende[:PORT]>`, Labels nach `#` und Kommentarzeilen. |
| FR-10 | M | Report-Generator (`ssh_matrix_report.py`) erzeugt aus `detail.csv`: `matrix.csv` (Host-Pivot) und `report.xlsx`. |
| FR-11 | M | `report.xlsx` enthält Sheets: **Suche** (Dropdowns VON/NACH mit Vorschlägen, Ergebnis per `INDEX`/`MATCH`), **Matrix**, **Netz-Matrix**, **Detail**, **Subnetze**, **Quelle**, **Liste**. |
| FR-12 | M | Status-Klassifikation: `auth_ok`, `auth_fail`, `port_open`, `port_closed`, `net_unreachable`, `source_unreachable`, `no_tool`, `tool_error`, `unclear`, `skipped`. |
| FR-13 | S | `--retry-status S1,S2,…` testet gezielt Paare mit bestimmten Status neu; `--retry-all-failed` als Shortcut (alles außer `auth_ok`/`skipped`). |
| FR-14 | S | `--subnet-quota N`: pro Richtung (`src_net → tgt_net`) werden nach N funktionierenden Quell-Hosts die restlichen Paare als `skipped` übersprungen. `--quota-mode` wählt, was zählt: `auth_ok` (nur voller Login) oder `reachable` (netzwerkseitig erreichbar: `auth_ok`, `auth_fail`, `port_open`). |
| FR-15 | S | Intelligentes Subnetz-Grouping per **Gap-Clustering** (`--subnet-gap`, Default 16): Netze werden aus der IP-Verteilung geschätzt, nicht starr als /24 angenommen. `--subnet-gap 0` = feste /24. |
| FR-16 | S | **Netz-Matrix**: Subnetz-Pivot (Zeilen = Quell-Netze, Spalten = Ziel-Netze) als Sheet + `netz_matrix.csv`, Zellen `ok/tested` mit Farbcodierung. |
| FR-17 | K | Farbcodierte Terminal-Ausgabe (Log-Zeilen + Live-Status-Zähler im Fortschrittsbalken), nur bei TTY, `NO_COLOR=1` deaktiviert. |
| FR-18 | K | `--limit-pairs N` für Trockenläufe (nur die ersten N Paare testen). |
| FR-19 | K | IP-Abfrage-Werkzeug (`ssh_matrix_query.py` geplant/optional): für eine IP alle Ziele bzw. alle Quellen anzeigen (HTML + CLI). |

---

## 3. Nicht-funktionale Anforderungen

| ID | Priorität | Anforderung |
|---|---|---|
| NFR-1 | M | Läuft auf **Kali Linux** (Python ≥ 3.10) mit `paramiko`, `openpyxl`, `tqdm`. |
| NFR-2 | M | **Keine dauerhafte Modifikation der Zielsysteme**: kein SSH-Key-Deploy, keine authorized_keys-Änderungen. Einzige Datei auf Zielen: temporäres askpass-Skript in `/tmp` oder `/dev/shm` (Mode 700), wird sofort gelöscht. |
| NFR-3 | M | **Passwort-Sicherheit**: nie als CLI-Argument (sonst in `ps` sichtbar); nur Umgebungsvariable oder Datei. Auf Ziel A steht das Passwort nur in der Umgebungsvariable des ssh-Prozesses (nicht in der /tmp-Datei). |
| NFR-4 | M | **Skalierung**: ~48.000 Tests (220 IPs) in realistischer Zeit (ca. 2,5–3,5 h bei Defaults); sanfte Parallelität zur Vermeidung von fail2ban/Rate-Limits. |
| NFR-5 | M | **Abbruchsicherheit**: inkrementelle CSV + `--resume`; Retry-Prune atomar (Temp-Datei + Rename). |
| NFR-6 | M | IPv4-only (keine IPv6-Unterstützung). |
| NFR-7 | S | Der Test führt auf Ziel B **nur** `echo CONN_OK_…` aus — keine Systemänderung. |
| NFR-8 | S | Keine known_hosts-Verschmutzung: `StrictHostKeyChecking=no`, `UserKnownHostsFile=/dev/null`. Host-Key-Änderungen werden bewusst ignoriert. |
| NFR-9 | S | Fortschrittsanzeige ehrlich: Resume-Offset (`done/total`), Rate nur aus echten SSH-Tests (`real/s`), `instant`-Zähler für sofort abgeschlossene Paare. |
| NFR-10 | K | `run.log` mit Laufprotokoll (Tool-Erkennung, Reconnects, Warnungen). |

---

## 4. Randbedingungen

- **Gleiche Credentials** auf allen IPs (User + Passwort). Abweichende
  Credentials sind nicht vorgesehen.
- **SSH-Port**: Default 22, abweichende Ports nur über `IP:PORT` in der
  Liste (Port gilt für beide Richtungen: Kali → A auf A's Port, A → B auf
  B's Port).
- Auf den Zielen muss `/tmp` (oder `/dev/shm`) **beschreibbar und
  ausführbar** sein, damit der askpass-Fallback (Rang 2) greifen kann.
- Die Testumgebung ist die Kali-Maschine selbst; das Kali wird nicht als
  Quelle oder Ziel getestet.
- Netzwerk-Firewalls zwischen den Netzen können zu `net_unreachable`
  führen — das ist ein relevantes Ergebnis, kein Fehler.

---

## 5. Abgrenzung (nicht im Scope)

- Kein SSH-Key-Management / Deploy.
- Keine Credential-Enumeration (kein Brute-Force, keine Passwort-Listen).
- Keine IPv6-Unterstützung.
- Keine kontinuierliche Überwachung (Einmaltest + Reports).
- Kein Web-Server/Service für die IP-Abfrage (statische HTML-Datei).
