#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ssh_matrix_report.py - baut aus detail.csv (Output von ssh_matrix.py) eine
Matrix-CSV und eine Excel-Datei (report.xlsx) mit Dropdown-Suche VON -> NACH.

Sheets in report.xlsx:
  Suche    - Dropdowns fuer VON/NACH (Datenvalidierung, Vorschlaege beim Tippen),
             Ergebnis via INDEX/MATCH auf dem Matrix-Sheet, Legende.
  Matrix   - Pivot: Zeilen = Quelle, Spalten = Ziel, Zelle = Status-Code mit Farbe.
  Detail   - alle Testzeilen mit AutoFilter und bedingter Formatierung.
  Subnetze - Statistik pro /24-Gruppe.
  Quelle   - Endpunkt-Mapping (IP, Port, Label, /24, /16).
  Liste    - Referenzliste der Endpunkte (Datenquelle fuer die Dropdowns).
"""

import argparse
import csv
import ipaddress
import os
import sys
from collections import defaultdict

try:
    from openpyxl import Workbook
    from openpyxl.formatting.rule import CellIsRule
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation
except ImportError:
    print("FEHLER: openpyxl fehlt. Auf dem Kali-Host installieren:", file=sys.stderr)
    print("  sudo apt install -y python3-openpyxl", file=sys.stderr)
    sys.exit(2)

DEFAULT_PORT = 22

VERSION = "v1.2.1"
AUTHOR = "Alex & DeepSeek"


def print_banner(stream=sys.stderr) -> None:
    """Start-Banner mit Version und Autoren-Hinweis."""
    line = "=" * 44
    print(line, file=stream)
    print(f"   SSH-Matrix-Tester {VERSION} - Report-Generator", file=stream)
    print(f"   entwickelt von {AUTHOR}", file=stream)
    print(line, file=stream)


CODE = {
    "auth_ok": "OK",
    "auth_fail": "AUTH",
    "port_open": "PORT",
    "port_closed": "CLOSED",
    "net_unreachable": "UNREACH",
    "source_unreachable": "SRCERR",
    "no_tool": "NOTOOL",
    "tool_error": "ERR",
    "unclear": "?",
    "skipped": "SKIP",
}

# Status -> (Hintergrund, Schrift)
STYLE = {
    "OK":     ("C6EFCE", "006100"),
    "AUTH":   ("FFEB9C", "9C6500"),
    "PORT":   ("DDEBF7", "1F4E79"),
    "CLOSED": ("D9D9D9", "404040"),
    "UNREACH": ("FFC7CE", "9C0006"),
    "SRCERR": ("F8CBAD", "833C00"),
    "NOTOOL": ("FCE4D6", "974706"),
    "ERR":    ("FCE4D6", "974706"),
    "?":      ("FFFFFF", "000000"),
    "SKIP":   ("E7E6E6", "595959"),
}


def load_detail(path: str) -> list:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def ep_label(ip: str, port: str) -> str:
    return ip if int(port) == DEFAULT_PORT else f"{ip}:{port}"


def build_endpoints(rows: list) -> list:
    eps = {}
    for r in rows:
        for side in ("source", "target"):
            key = (r[f"{side}_ip"], int(r[f"{side}_port"]))
            if key not in eps:
                eps[key] = {
                    "ip": r[f"{side}_ip"],
                    "port": int(r[f"{side}_port"]),
                    "label": r.get(f"{side}_label", "") or "",
                    "n24": r.get(f"{side}_24", r.get("src_24", "")),
                    "n16": r.get(f"{side}_16", r.get("src_16", "")),
                    "net": r.get(f"{'src' if side == 'source' else 'tgt'}_net",
                                 r.get(f"{side}_24", "")),
                }
            if not eps[key]["label"] and r.get(f"{side}_label"):
                eps[key]["label"] = r[f"{side}_label"]
    return sorted(eps.values(),
                  key=lambda e: (ipaddress.IPv4Address(e["ip"]).packed, e["port"]))


def status_map(rows: list) -> dict:
    sm = {}
    for r in rows:
        sm[(r["source_ip"], int(r["source_port"]),
            r["target_ip"], int(r["target_port"]))] = r["status"]
    return sm


def write_matrix_csv(matrix_path: str, eps: list, sm: dict) -> None:
    labels = [ep_label(e["ip"], str(e["port"])) for e in eps]
    with open(matrix_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["quelle\\ziel"] + labels)
        for s in eps:
            row = [ep_label(s["ip"], str(s["port"]))]
            for t in eps:
                if s["ip"] == t["ip"] and s["port"] == t["port"]:
                    row.append("")
                    continue
                st = sm.get((s["ip"], s["port"], t["ip"], t["port"]))
                row.append(CODE.get(st, "?") if st else "")
            w.writerow(row)


def fill(hex_color: str) -> PatternFill:
    return PatternFill(start_color=hex_color, end_color=hex_color, fill_type="solid")


def build_subnet_agg(rows: list) -> tuple:
    """Aggregiert detail.csv nach (src_net, tgt_net).
    Return: (agg dict, sortierte Netz-Liste)."""
    agg = defaultdict(lambda: {"ok": 0, "port_only": 0, "failed": 0, "tested": 0, "skipped": 0})
    nets = set()
    for r in rows:
        s_net = r.get("src_net") or r.get("src_24", "")
        t_net = r.get("tgt_net") or r.get("tgt_24", "")
        if not s_net or not t_net:
            continue
        nets.add(s_net)
        nets.add(t_net)
        a = agg[(s_net, t_net)]
        st = r["status"]
        if st == "skipped":
            a["skipped"] += 1
        else:
            a["tested"] += 1
            if st == "auth_ok":
                a["ok"] += 1
            elif st == "port_open":
                a["port_only"] += 1
            else:
                a["failed"] += 1
    sorted_nets = sorted(nets, key=lambda n: ipaddress.IPv4Network(n, strict=False).network_address.packed)
    return agg, sorted_nets


def subnet_cell(agg_entry: dict) -> tuple:
    """Return (text, bg_color, font_color) fuer eine Netz-Matrix-Zelle."""
    if not agg_entry or agg_entry["tested"] == 0:
        text = "–" if agg_entry and agg_entry["skipped"] > 0 else ""
        return text, "E7E6E6", "595959"
    ok = agg_entry["ok"]
    tested = agg_entry["tested"]
    text = f"{ok}/{tested}"
    if ok > 0:
        return text, "C6EFCE", "006100"
    if agg_entry["port_only"] > 0:
        return text, "FFEB9C", "9C6500"
    return text, "FFC7CE", "9C0006"


def write_netz_matrix_csv(path: str, agg: dict, nets: list) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["quelle\\ziel"] + nets)
        for s in nets:
            row = [s]
            for t in nets:
                if s == t:
                    row.append("")
                    continue
                a = agg.get((s, t))
                if not a or a["tested"] == 0:
                    row.append("–" if a and a["skipped"] > 0 else "")
                else:
                    row.append(f"{a['ok']}/{a['tested']}")
            w.writerow(row)


def build_xlsx(xlsx_path: str, detail_path: str, rows: list, eps: list,
               sm: dict, labels: list, agg: dict = None, nets: list = None) -> None:
    wb = Workbook()
    ws_search = wb.active
    ws_search.title = "Suche"
    ws_matrix = wb.create_sheet("Matrix")
    ws_netz = wb.create_sheet("Netz-Matrix") if agg else None
    ws_detail = wb.create_sheet("Detail")
    ws_subnets = wb.create_sheet("Subnetze")
    ws_quelle = wb.create_sheet("Quelle")
    ws_liste = wb.create_sheet("Liste")

    n = len(eps)
    last_col = get_column_letter(1 + n)
    last_row = 1 + n

    # ---- Liste (Dropdown-Quelle) --------------------------------------
    ws_liste["A1"] = "Endpunkt (Auswahl fuer Suche)"
    ws_liste["A1"].font = Font(bold=True)
    for i, lab in enumerate(labels, start=2):
        ws_liste.cell(row=i, column=1, value=lab)

    # ---- Suche ---------------------------------------------------------
    ws_search["A1"] = "SSH-Matrix - Suche VON -> NACH"
    ws_search["A1"].font = Font(bold=True, size=14)
    ws_search["A3"] = "VON"
    ws_search["A3"].font = Font(bold=True)
    ws_search["A4"] = "NACH"
    ws_search["A4"].font = Font(bold=True)
    ws_search["A5"] = "Ergebnis"
    ws_search["A5"].font = Font(bold=True)
    ws_search["C3"] = "Quell-Endpunkt (Dropdown / Tippen)"
    ws_search["C4"] = "Ziel-Endpunkt (Dropdown / Tippen)"
    ws_search["C5"] = "Status aus der Matrix"

    formula = (
        '=IF(OR($B$3="",$B$4=""),"",IFERROR('
        f'INDEX(Matrix!$B$2:${last_col}${last_row},'
        f'MATCH($B$3,Matrix!$A$2:$A${last_row},0),'
        f'MATCH($B$4,Matrix!$B$1:${last_col}$1,0)),"nicht getestet"))'
    )
    ws_search["B5"] = formula
    ws_search["B5"].font = Font(bold=True, size=12)

    dv = DataValidation(type="list", formula1=f"=Liste!$A$2:$A${last_row}",
                        allow_blank=True, showErrorMessage=False)
    ws_search.add_data_validation(dv)
    dv.add("B3")
    dv.add("B4")

    # Legende
    ws_search["A7"] = "Legende:"
    ws_search["A7"].font = Font(bold=True)
    legend = [
        ("OK", "Voller SSH-Login von A nach B erfolgreich"),
        ("AUTH", "SSH-Port offen, aber Login abgelehnt"),
        ("PORT", "Nur Port 22 erreichbar (kein SSH-Login getestet)"),
        ("CLOSED", "Port zu (Connection refused)"),
        ("UNREACH", "Netzwerk nicht erreichbar (Timeout/No Route)"),
        ("SRCERR", "Quelle war vom Kali aus nicht erreichbar"),
        ("NOTOOL", "Auf der Quelle kein ssh/nc/bash verfuegbar"),
        ("ERR", "Fehler (Detail-Sheet/error-Spalte beachten)"),
        ("SKIP", "Uebersprungen (Subnetz-Quota erreicht)"),
        ("?", "Undefiniert / nicht getestet"),
    ]
    for i, (code, desc) in enumerate(legend, start=8):
        ws_search.cell(row=i, column=1, value=code)
        ws_search.cell(row=i, column=2, value=desc)
        if code in STYLE:
            bg, fg = STYLE[code]
            ws_search.cell(row=i, column=1).fill = fill(bg)
            ws_search.cell(row=i, column=1).font = Font(color=fg, bold=True)

    ws_search.column_dimensions["A"].width = 12
    ws_search.column_dimensions["B"].width = 22
    ws_search.column_dimensions["C"].width = 40

    # ---- Matrix ---------------------------------------------------------
    ws_matrix["A1"] = "Quelle \\ Ziel"
    ws_matrix["A1"].font = Font(bold=True)
    for j, lab in enumerate(labels, start=2):
        c = ws_matrix.cell(row=1, column=j, value=lab)
        c.font = Font(bold=True, size=8)
        c.alignment = Alignment(textRotation=90, horizontal="center")
        ws_matrix.column_dimensions[get_column_letter(j)].width = 7
    for i, s in enumerate(eps, start=2):
        ws_matrix.cell(row=i, column=1, value=ep_label(s["ip"], str(s["port"]))).font = Font(bold=True)
        for j, t in enumerate(eps, start=2):
            if s["ip"] == t["ip"] and s["port"] == t["port"]:
                continue
            st = sm.get((s["ip"], s["port"], t["ip"], t["port"]))
            code = CODE.get(st, "?") if st else "?"
            cell = ws_matrix.cell(row=i, column=j, value=code)
            cell.alignment = Alignment(horizontal="center")
            if code in STYLE:
                bg, fg = STYLE[code]
                cell.fill = fill(bg)
                cell.font = Font(color=fg, size=8)
    ws_matrix.column_dimensions["A"].width = 15
    ws_matrix.freeze_panes = "B2"

    # ---- Netz-Matrix ---------------------------------------------------
    if ws_netz is not None and nets:
        ws_netz["A1"] = "Quell-Netz \\ Ziel-Netz"
        ws_netz["A1"].font = Font(bold=True)
        for j, t_net in enumerate(nets, start=2):
            c = ws_netz.cell(row=1, column=j, value=t_net)
            c.font = Font(bold=True, size=8)
            c.alignment = Alignment(textRotation=90, horizontal="center")
            ws_netz.column_dimensions[get_column_letter(j)].width = 11
        for i, s_net in enumerate(nets, start=2):
            ws_netz.cell(row=i, column=1, value=s_net).font = Font(bold=True, size=8)
            for j, t_net in enumerate(nets, start=2):
                if s_net == t_net:
                    continue
                text, bg, fg = subnet_cell(agg.get((s_net, t_net)))
                cell = ws_netz.cell(row=i, column=j, value=text)
                cell.alignment = Alignment(horizontal="center")
                cell.fill = fill(bg)
                cell.font = Font(color=fg, size=8)
        ws_netz.column_dimensions["A"].width = 18
        ws_netz.freeze_panes = "B2"

    # ---- Detail ---------------------------------------------------------
    if rows:
        headers = list(rows[0].keys())
        ws_detail.append(headers)
        for cell in ws_detail[1]:
            cell.font = Font(bold=True)
        for r in rows:
            ws_detail.append([r.get(h, "") for h in headers])
        ws_detail.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{len(rows) + 1}"
        ws_detail.freeze_panes = "A2"

        status_col = headers.index("status") + 1
        col_letter = get_column_letter(status_col)
        rng = f"{col_letter}2:{col_letter}{len(rows) + 1}"
        for st, code in CODE.items():
            if code not in STYLE:
                continue
            bg, fg = STYLE[code]
            ws_detail.conditional_formatting.add(
                rng,
                CellIsRule(operator="equal", formula=[f'"{st}"'],
                           fill=fill(bg), font=Font(color=fg)))
        widths = {"timestamp": 22, "source_ip": 15, "source_port": 9, "source_label": 18,
                  "src_24": 13, "src_16": 13, "src_net": 16, "target_ip": 15,
                  "target_port": 9, "target_label": 18, "tgt_24": 13, "tgt_16": 13,
                  "tgt_net": 16, "direction": 9, "method": 10, "status": 13,
                  "latency_ms": 11, "error": 60}
        for idx, h in enumerate(headers, start=1):
            ws_detail.column_dimensions[get_column_letter(idx)].width = widths.get(h, 14)

    # ---- Subnetze -------------------------------------------------------
    src_ok = set()
    tgt_reach = set()
    tgt_auth = set()
    skipped_pairs = 0
    for r in rows:
        s = (r["source_ip"], int(r["source_port"]))
        t = (r["target_ip"], int(r["target_port"]))
        if r["status"] == "auth_ok":
            src_ok.add(s)
            tgt_auth.add(t)
        if r["status"] in ("auth_ok", "port_open"):
            tgt_reach.add(t)
        if r["status"] == "skipped":
            skipped_pairs += 1

    ep_by_n24 = defaultdict(list)
    for e in eps:
        ep_by_n24[e["n24"]].append(e)

    ws_subnets.append(["/24-Subnetz", "Endpunkte", "Quelle mit Login-Erfolg",
                       "als Ziel erreichbar (Login oder Port)", "als Ziel mit Login-Erfolg",
                       "skipped (Quota)"])
    for cell in ws_subnets[1]:
        cell.font = Font(bold=True)
    for n24 in sorted(ep_by_n24):
        members = ep_by_n24[n24]
        keys = [(m["ip"], m["port"]) for m in members]
        n_skip = sum(1 for r in rows
                     if r.get("src_24") == n24 and r["status"] == "skipped")
        row = [
            n24,
            len(members),
            sum(1 for k in keys if k in src_ok),
            sum(1 for k in keys if k in tgt_reach),
            sum(1 for k in keys if k in tgt_auth),
            n_skip,
        ]
        ws_subnets.append(row)
    ws_subnets.auto_filter.ref = f"A1:F{len(ep_by_n24) + 1}"
    ws_subnets.freeze_panes = "A2"
    for col, w in zip("ABCDEF", (15, 10, 28, 34, 26, 14)):
        ws_subnets.column_dimensions[col].width = w

    # ---- Quelle ---------------------------------------------------------
    ws_quelle.append(["IP", "Port", "Label", "/24", "/16", "Netz"])
    for cell in ws_quelle[1]:
        cell.font = Font(bold=True)
    for e in eps:
        ws_quelle.append([e["ip"], e["port"], e["label"], e["n24"], e["n16"], e.get("net", "")])
    ws_quelle.auto_filter.ref = f"A1:F{len(eps) + 1}"
    ws_quelle.freeze_panes = "A2"
    for col, w in zip("ABCDEF", (15, 8, 24, 13, 13, 16)):
        ws_quelle.column_dimensions[col].width = w

    wb.save(xlsx_path)


def main():
    ap = argparse.ArgumentParser(description="Erzeugt Matrix-CSV + report.xlsx aus detail.csv")
    ap.add_argument("--version", action="version",
                    version=f"%(prog)s {VERSION} (entwickelt von {AUTHOR})")
    ap.add_argument("--detail", required=True, help="Pfad zu detail.csv")
    ap.add_argument("--out", default="ssh_matrix_out",
                    help="Ausgabe-Verzeichnis (Default: ssh_matrix_out)")
    ap.add_argument("--matrix-name", default="matrix.csv")
    ap.add_argument("--xlsx-name", default="report.xlsx")
    args = ap.parse_args()

    print_banner()

    if not os.path.exists(args.detail):
        print(f"FEHLER: {args.detail} existiert nicht", file=sys.stderr)
        sys.exit(1)

    rows = load_detail(args.detail)
    if not rows:
        print(f"FEHLER: {args.detail} enthaelt keine Daten", file=sys.stderr)
        sys.exit(1)

    eps = build_endpoints(rows)
    sm = status_map(rows)
    labels = [ep_label(e["ip"], str(e["port"])) for e in eps]
    agg, nets = build_subnet_agg(rows)

    os.makedirs(args.out, exist_ok=True)
    matrix_path = os.path.join(args.out, args.matrix_name)
    netz_matrix_path = os.path.join(args.out, "netz_matrix.csv")
    xlsx_path = os.path.join(args.out, args.xlsx_name)

    write_matrix_csv(matrix_path, eps, sm)
    write_netz_matrix_csv(netz_matrix_path, agg, nets)
    build_xlsx(xlsx_path, args.detail, rows, eps, sm, labels, agg, nets)

    print(f"Matrix-CSV    : {matrix_path}")
    print(f"Netz-Matrix-CSV: {netz_matrix_path}")
    print(f"Excel-Report  : {xlsx_path}")
    print(f"  Endpunkte : {len(eps)}")
    print(f"  Netze     : {len(nets)}")
    print(f"  Zeilen    : {len(rows)}")


if __name__ == "__main__":
    main()
