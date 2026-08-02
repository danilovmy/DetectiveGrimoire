#!/usr/bin/env python3
"""Create a local XLSX copy with source strings from the user's game files."""
from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
XML_NS = "http://www.w3.org/XML/1998/namespace"
ET.register_namespace("", SHEET_NS)
ET.register_namespace("r", REL_NS)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workbook", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def column_index(cell_ref: str) -> int:
    value = 0
    for char in cell_ref:
        if char.isalpha():
            value = value * 26 + ord(char.upper()) - 64
    return value - 1


def column_name(index: int) -> str:
    result = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def cell_value(cell: ET.Element, shared: list[str]) -> str:
    kind = cell.attrib.get("t")
    if kind == "inlineStr":
        node = cell.find(f"{{{SHEET_NS}}}is")
        return "" if node is None else "".join(node.itertext())
    value = cell.find(f"{{{SHEET_NS}}}v")
    if value is None:
        return ""
    if kind == "s":
        return shared[int(value.text or "0")]
    return value.text or ""


def sheet_path(archive: zipfile.ZipFile) -> str:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {item.attrib["Id"]: item.attrib["Target"] for item in relationships if item.tag == f"{{{PKG_REL_NS}}}Relationship"}
    for sheet in workbook.findall(f"{{{SHEET_NS}}}sheets/{{{SHEET_NS}}}sheet"):
        if sheet.attrib.get("name") == "Перевод":
            target = targets[sheet.attrib[f"{{{REL_NS}}}id"]].lstrip("/")
            return target if target.startswith("xl/") else "xl/" + target
    raise ValueError("Worksheet 'Перевод' is missing")


def source_by_key(catalog: Path) -> dict[str, str]:
    output: dict[str, str] = {}
    for line in catalog.read_text(encoding="utf-8").splitlines():
        item = json.loads(line)
        key, source = item["translation_key"], item["source"]
        if key in output and output[key] != source:
            raise ValueError(f"Conflicting source strings for {key}")
        output[key] = source
    return output


def fill(workbook: Path, catalog: Path, output: Path) -> int:
    sources = source_by_key(catalog)
    with zipfile.ZipFile(workbook) as archive:
        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared = ["".join(item.itertext()) for item in ET.fromstring(archive.read("xl/sharedStrings.xml")).findall(f"{{{SHEET_NS}}}si")]
        worksheet_path = sheet_path(archive)
        tree = ET.fromstring(archive.read(worksheet_path))
        entries = [(item, archive.read(item.filename)) for item in archive.infolist()]

    rows = tree.findall(f".//{{{SHEET_NS}}}sheetData/{{{SHEET_NS}}}row")
    if not rows:
        raise ValueError("Translation sheet is empty")
    header = {cell_value(cell, shared): column_index(cell.attrib["r"]) for cell in rows[0].findall(f"{{{SHEET_NS}}}c")}
    for required in ("Ключ", "Оригинал"):
        if required not in header:
            raise ValueError(f"Workbook column {required!r} is missing")

    updated = 0
    for row in rows[1:]:
        cells = {column_index(cell.attrib["r"]): cell for cell in row.findall(f"{{{SHEET_NS}}}c")}
        key = cell_value(cells[header["Ключ"]], shared) if header["Ключ"] in cells else ""
        source = sources.get(key)
        if source is None:
            continue
        original = cells.get(header["Оригинал"])
        if original is None:
            original = ET.SubElement(row, f"{{{SHEET_NS}}}c", {"r": f"{column_name(header['Оригинал'])}{row.attrib['r']}"})
        for child in list(original):
            original.remove(child)
        original.attrib["t"] = "inlineStr"
        inline = ET.SubElement(original, f"{{{SHEET_NS}}}is")
        text = ET.SubElement(inline, f"{{{SHEET_NS}}}t")
        if source[:1].isspace() or source[-1:].isspace():
            text.attrib[f"{{{XML_NS}}}space"] = "preserve"
        text.text = source
        updated += 1

    changed = ET.tostring(tree, encoding="utf-8", xml_declaration=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for info, data in entries:
            archive.writestr(info, changed if info.filename == worksheet_path else data)
    return updated


def main() -> int:
    args = parse_args()
    if not args.workbook.exists() or not args.catalog.exists():
        raise FileNotFoundError("Workbook or local catalog was not found")
    if args.output.resolve() == args.workbook.resolve():
        raise ValueError("Use a separate --output path; keep the repository workbook clean")
    count = fill(args.workbook, args.catalog, args.output)
    print(f"[done] Filled {count} local source strings: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
