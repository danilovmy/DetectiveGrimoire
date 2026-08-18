#!/usr/bin/env python3
"""Make a distributable XLSX containing only technical keys and Russian translations."""
from __future__ import annotations

import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.sax.saxutils import escape

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS = {"m": MAIN, "r": REL}

def column_index(reference: str) -> int:
    result = 0
    for char in reference:
        if char.isalpha(): result = result * 26 + ord(char.upper()) - 64
    return result - 1

def cell_text(cell: ET.Element, shared: list[str]) -> str:
    if cell.get("t") == "s":
        node = cell.find(f"{{{MAIN}}}v")
        return shared[int(node.text)] if node is not None and node.text else ""
    return "".join(cell.itertext())

def extract_translations(source: Path) -> list[tuple[str, str]]:
    with zipfile.ZipFile(source) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {item.get("Id"): item.get("Target") for item in rels}
        sheet_path = None
        for sheet in workbook.findall("m:sheets/m:sheet", NS):
            if sheet.get("name") == "Перевод":
                target = targets[sheet.get(f"{{{REL}}}id")].lstrip("/")
                sheet_path = target if target.startswith("xl/") else "xl/" + target
                break
        if not sheet_path: raise ValueError("Worksheet 'Перевод' is missing")
        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared = ["".join(item.itertext()) for item in ET.fromstring(archive.read("xl/sharedStrings.xml")).findall(f"{{{MAIN}}}si")]
        rows = ET.fromstring(archive.read(sheet_path)).findall(".//m:sheetData/m:row", NS)
        if not rows: raise ValueError("Worksheet 'Перевод' is empty")
        header = {cell_text(c, shared).strip(): column_index(c.get("r", "A1")) for c in rows[0].findall("m:c", NS)}
        if {"Ключ", "Перевод"} - header.keys(): raise ValueError("Worksheet 'Перевод' must contain 'Ключ' and 'Перевод'")
        output = []
        for row in rows[1:]:
            values = {column_index(c.get("r", "A1")): cell_text(c, shared) for c in row.findall("m:c", NS)}
            key = values.get(header["Ключ"], "").strip(); translation = values.get(header["Перевод"], "")
            if key and translation.strip(): output.append((key, translation))
        return output

def cell(reference: str, value: str) -> str:
    return f'<c r="{reference}" t="inlineStr"><is><t xml:space="preserve">{escape(value)}</t></is></c>'

def main(source: Path, target: Path) -> None:
    translations = extract_translations(source)
    if not translations: raise ValueError("No Russian translations found")
    rows = [f'<row r="1">{cell("A1", "Ключ")}{cell("B1", "Перевод")}</row>']
    rows.extend(f'<row r="{n}">{cell(f"A{n}", key)}{cell(f"B{n}", text)}</row>' for n, (key, text) in enumerate(translations, 2))
    sheet = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="{MAIN}"><cols><col min="1" max="1" width="72" customWidth="1"/><col min="2" max="2" width="68" customWidth="1"/></cols><sheetData>{"".join(rows)}</sheetData></worksheet>'
    workbook = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="{MAIN}" xmlns:r="{REL}"><sheets><sheet name="Перевод" sheetId="1" r:id="rId1"/></sheets></workbook>'
    rels = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="{REL}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>'
    root_rels = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'
    types = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>'
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", types); archive.writestr("_rels/.rels", root_rels)
        archive.writestr("xl/workbook.xml", workbook); archive.writestr("xl/_rels/workbook.xml.rels", rels); archive.writestr("xl/worksheets/sheet1.xml", sheet)

if __name__ == "__main__":
    if len(sys.argv) != 3: raise SystemExit("Usage: sanitize_workbook.py INPUT.xlsx OUTPUT.xlsx")
    main(Path(sys.argv[1]), Path(sys.argv[2]))
