#!/usr/bin/env python3
"""Apply a translated catalog to Detective Grimoire SWF resources safely."""
from __future__ import annotations

import argparse, datetime as dt, hashlib, json, lzma, shutil, subprocess, sys, tempfile, zipfile, zlib
from collections import defaultdict
from pathlib import Path
from typing import Iterator
from xml.etree import ElementTree as ET

RECORD_SEPARATOR = "\r\n--- RECORDSEPARATOR ---\r\n"
MAIN_SWF = "DetectiveGrimoireDesktopVanilla.swf"
SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True); p.add_argument("--translations", type=Path, required=True)
    p.add_argument("--catalog", type=Path)
    p.add_argument("--java", type=Path, required=True); p.add_argument("--ffdec-jar", type=Path, required=True); p.add_argument("--font", type=Path, required=True); p.add_argument("--font-name", default="Arial")
    p.add_argument("--backup-dir", type=Path, required=True); p.add_argument("--dry-run", action="store_true")
    p.add_argument("--resource", action="append", help="Apply only one or more static SWF resources")
    return p.parse_args()

def column_index(cell_ref: str) -> int:
    value = 0
    for char in cell_ref:
        if char.isalpha(): value = value * 26 + ord(char.upper()) - 64
    return value - 1

def read_xlsx_sheet(path: Path, expected_name: str) -> list[list[str]]:
    with zipfile.ZipFile(path) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml")); rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {r.attrib["Id"]: r.attrib["Target"] for r in rels if r.tag.endswith("Relationship")}
        sheet_path = None
        for sheet in workbook.findall(f"{{{SHEET_NS}}}sheets/{{{SHEET_NS}}}sheet"):
            if sheet.attrib.get("name") == expected_name:
                target = targets[sheet.attrib[f"{{{REL_NS}}}id"]].lstrip("/")
                sheet_path = target if target.startswith("xl/") else "xl/" + target; break
        if not sheet_path: raise ValueError(f"Worksheet {expected_name!r} is missing")
        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared = ["".join(item.itertext()) for item in ET.fromstring(archive.read("xl/sharedStrings.xml")).findall(f"{{{SHEET_NS}}}si")]
        output = []
        for row in ET.fromstring(archive.read(sheet_path)).findall(f".//{{{SHEET_NS}}}sheetData/{{{SHEET_NS}}}row"):
            values = {}
            for cell in row.findall(f"{{{SHEET_NS}}}c"):
                idx = column_index(cell.attrib.get("r", "A1")); value = cell.find(f"{{{SHEET_NS}}}v")
                if cell.attrib.get("t") == "s" and value is not None: values[idx] = shared[int(value.text or "0")]
                elif cell.attrib.get("t") == "inlineStr":
                    inline = cell.find(f"{{{SHEET_NS}}}is"); values[idx] = "" if inline is None else "".join(inline.itertext())
                else: values[idx] = "" if value is None else value.text or ""
            output.append([values.get(i, "") for i in range(max(values, default=-1) + 1)])
        return output

def load_translations(path: Path) -> dict[str, str]:
    rows = read_xlsx_sheet(path, "Перевод")
    if not rows: raise ValueError("Translation workbook is empty")
    headers = {name: i for i, name in enumerate(rows[0])}
    if missing := {"Ключ", "Перевод"} - headers.keys(): raise ValueError(f"Missing workbook columns: {sorted(missing)}")
    output = {}
    for number, row in enumerate(rows[1:], 2):
        key = row[headers["Ключ"]] if len(row) > headers["Ключ"] else ""; text = row[headers["Перевод"]] if len(row) > headers["Перевод"] else ""
        if key and text.strip():
            if key in output and output[key] != text: raise ValueError(f"Conflicting translation for {key} in row {number}")
            output[key] = text
    if not output: raise ValueError("No non-empty translations found")
    return output

def read_u30(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 35, 7):
        byte = data[pos]; pos += 1; value |= (byte & 127) << shift
        if not byte & 128: return value, pos
    raise ValueError("Invalid U30")

def write_u30(value: int) -> bytes:
    result = bytearray()
    while True:
        byte = value & 127; value >>= 7; result.append(byte | (128 if value else 0))
        if not value: return bytes(result)

def split_styled_text(text: str, segments: list[str]) -> list[str]:
    if len(segments) <= 1: return [text]
    weights = [max(1, len(x)) for x in segments]; remaining = text; remaining_weight = sum(weights); result = []
    for weight in weights[:-1]:
        cut = max(0, min(len(remaining), round(len(remaining) * weight / remaining_weight)))
        # Original SWF style records can be placed on different lines. Never
        # split a translated Russian word between records: when the nearby
        # window has no boundary, continue to the next word end instead.
        right = remaining.find(" ", cut)
        left = remaining.rfind(" ", 0, cut)
        if right >= 0:
            cut = right + 1
        elif left >= 0:
            cut = left + 1
        else:
            cut = len(remaining)
        result.append(remaining[:cut]); remaining = remaining[cut:]; remaining_weight -= weight
    # Java's String.split drops trailing empty records. A literal space keeps
    # every original style segment present while ensuring no English remainder
    # from a now-empty segment survives the import.
    return [part if part else " " for part in [*result, remaining]]

def run_ffdec(java: Path, jar: Path, source: Path, output: Path, text_folder: Path) -> None:
    done = subprocess.run([str(java), "-jar", str(jar), "-config", "resetLetterSpacingOnTextImport=true", "-format", "text:plain", "-onerror", "abort", "-importText", str(source), str(output), str(text_folder)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    if done.returncode or not output.exists(): raise RuntimeError(f"FFDec import failed for {source}:\n{done.stdout[-6000:]}")

def font_ids_for_replacement(source: Path) -> list[int]:
    data, _ = decompress_swf(source.read_bytes())
    # DefineFont1/2/3 can be rebuilt by FFDec from a TTF. DefineFont4 carries
    # raw CFF data and must not be passed a TTF through -replace.
    return [int.from_bytes(payload[:2], "little") for code, _length, payload in iter_tags(data) if code in {10, 48, 75} and len(payload) >= 2]

def run_font_replace(java: Path, jar: Path, source: Path, output: Path, font: Path, font_ids: list[int]) -> None:
    if not font_ids:
        shutil.copy2(source, output)
        return
    command = [str(java), "-jar", str(jar), "-replace", str(source), str(output)]
    for font_id in font_ids:
        command.extend([str(font_id), str(font)])
    done = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    if done.returncode or not output.exists(): raise RuntimeError(f"FFDec font replacement failed for {source}:\n{done.stdout[-6000:]}")

def patch_font_names(source: Path, output: Path, font_ids: list[int], font_name: str) -> None:
    raw = source.read_bytes(); fws, prop = decompress_swf(raw); rebuilt = bytearray(fws[:tag_start(fws)]); encoded = font_name.encode("utf-8")
    for code, _length, payload in iter_tags(fws):
        new = payload
        if code in {48, 75} and len(payload) >= 5 and int.from_bytes(payload[:2], "little") in font_ids:
            old_length = payload[4]
            new = payload[:4] + bytes([len(encoded)]) + encoded + payload[5 + old_length:]
        elif code == 88 and len(payload) >= 3 and int.from_bytes(payload[:2], "little") in font_ids:
            old_end = payload.find(b"\0", 2)
            if old_end >= 0:
                new = payload[:2] + encoded + b"\0" + payload[old_end + 1:]
        header = (code << 6) | (len(new) if len(new) < 63 else 63); rebuilt += header.to_bytes(2, "little")
        if len(new) >= 63: rebuilt += len(new).to_bytes(4, "little")
        rebuilt += new
    output.write_bytes(compress_swf(bytes(rebuilt), raw[:3], prop))

def decompress_swf(raw: bytes) -> tuple[bytes, bytes | None]:
    sig = raw[:3]
    if sig == b"FWS": return raw, None
    if sig == b"CWS": return b"FWS" + raw[3:8] + zlib.decompress(raw[8:]), None
    if sig == b"ZWS":
        prop = raw[12:17]; n = prop[0]; lc = n % 9; quotient = n // 9; lp = quotient % 5; pb = quotient // 5
        filters = [{"id": lzma.FILTER_LZMA1, "dict_size": int.from_bytes(prop[1:5], "little"), "lc": lc, "lp": lp, "pb": pb}]
        return b"FWS" + raw[3:8] + lzma.decompress(raw[17:], format=lzma.FORMAT_RAW, filters=filters), prop
    raise ValueError(f"Unsupported SWF signature {sig!r}")

def compress_swf(fws: bytes, signature: bytes, prop: bytes | None) -> bytes:
    length = len(fws).to_bytes(4, "little")
    if signature == b"FWS": return b"FWS" + fws[3:4] + length + fws[8:]
    if signature == b"CWS": return b"CWS" + fws[3:4] + length + zlib.compress(fws[8:])
    if signature == b"ZWS" and prop:
        n = prop[0]; lc = n % 9; quotient = n // 9; lp = quotient % 5; pb = quotient // 5
        payload = lzma.compress(fws[8:], format=lzma.FORMAT_RAW, filters=[{"id": lzma.FILTER_LZMA1, "dict_size": int.from_bytes(prop[1:5], "little"), "lc": lc, "lp": lp, "pb": pb}])
        return b"ZWS" + fws[3:4] + length + len(payload).to_bytes(4, "little") + prop + payload
    raise ValueError("Cannot recompress SWF")

def tag_start(data: bytes) -> int:
    pos = 8; bits = data[pos] >> 3; return pos + (5 + bits * 4 + 7) // 8 + 4

def iter_tags(data: bytes) -> Iterator[tuple[int, int, bytes]]:
    pos = tag_start(data)
    while pos + 2 <= len(data):
        header = int.from_bytes(data[pos:pos+2], "little"); pos += 2; code, length = header >> 6, header & 63
        if length == 63: length = int.from_bytes(data[pos:pos+4], "little"); pos += 4
        payload = data[pos:pos+length]; pos += length; yield code, length, payload
        if code == 0: return

def patch_abc(abc: bytes, updates: dict[int, str]) -> bytes:
    pos = 4
    for width in (None, None, 8):
        count, pos = read_u30(abc, pos)
        for _ in range(max(0, count - 1)):
            if width is None: _, pos = read_u30(abc, pos)
            else: pos += width
    count, pool_start = read_u30(abc, pos); pos = pool_start; out = bytearray(abc[:pool_start])
    for index in range(1, count):
        length, pos = read_u30(abc, pos); source = abc[pos:pos+length]; pos += length; value = updates.get(index, source)
        if isinstance(value, str): value = value.encode("utf-8")
        out += write_u30(len(value)) + value
    return bytes(out) + abc[pos:]

def patch_main(source: Path, output: Path, updates: dict[str, dict[int, str]]) -> int:
    raw = source.read_bytes(); fws, prop = decompress_swf(raw); rebuilt = bytearray(fws[:tag_start(fws)]); count = 0
    for code, _length, payload in iter_tags(fws):
        new = payload
        if code == 82:
            end = payload.index(0, 4); name = payload[4:end].decode("utf-8", "replace") or "unnamed"
            if name in updates: new = payload[:end+1] + patch_abc(payload[end+1:], updates[name]); count += len(updates[name])
        head = (code << 6) | (len(new) if len(new) < 63 else 63); rebuilt += head.to_bytes(2, "little")
        if len(new) >= 63: rebuilt += len(new).to_bytes(4, "little")
        rebuilt += new
    output.write_bytes(compress_swf(bytes(rebuilt), raw[:3], prop)); return count

def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""): digest.update(block)
    return digest.hexdigest()

def main() -> int:
    args = parse_args(); root = args.root.resolve(); catalog = args.catalog or root / "localization" / "catalog" / "occurrences.jsonl"
    for item in (root / MAIN_SWF, args.translations, args.java, args.ffdec_jar, args.font, catalog):
        if not item.exists(): raise FileNotFoundError(item)
    translations = load_translations(args.translations); occurrences = [json.loads(x) for x in catalog.read_text(encoding="utf-8").splitlines() if x]
    known = {x["translation_key"] for x in occurrences}
    if unknown := set(translations) - known: raise ValueError(f"Workbook has {len(unknown)} unknown keys")
    static: dict[str, list[dict]] = defaultdict(list); dynamic: dict[str, dict[int, str]] = defaultdict(dict)
    for item in occurrences:
        text = translations.get(item["translation_key"])
        if not text: continue
        if item["kind"] == "swf_text": static[item["resource"]].append(item)
        if item["kind"] == "abc_string": dynamic[item["abc_name"]][item["abc_string_index"]] = text
    if args.resource:
        requested = set(args.resource)
        unknown = requested - set(static)
        if unknown:
            raise ValueError(f"No translated static text for: {sorted(unknown)}")
        static = defaultdict(list, {resource: items for resource, items in static.items() if resource in requested})
        dynamic = defaultdict(dict)
    files = set(static) | ({MAIN_SWF} if dynamic else set())
    print(f"[catalog] {len(translations)} translated keys; {sum(map(len, static.values()))} static and {sum(map(len, dynamic.values()))} dynamic occurrences")
    print(f"[plan] {len(files)} SWF files will be changed")
    if args.dry_run: return 0
    args.backup_dir.mkdir(parents=True, exist_ok=False); manifest = {"created_at": dt.datetime.now(dt.timezone.utc).isoformat(), "translations": str(args.translations), "files": []}
    with tempfile.TemporaryDirectory(prefix="grimoire-apply-", dir=root / "localization") as folder:
        work = Path(folder)
        for resource, items in sorted(static.items()):
            text_folder = work / "text" / resource
            tag_folder = text_folder / "texts"
            for item in items:
                tag_folder.mkdir(parents=True, exist_ok=True); (tag_folder / f"{item['tag_id']}.txt").write_text(RECORD_SEPARATOR.join(split_styled_text(translations[item['translation_key']], item['segments'])), encoding="utf-8-sig", newline="\n")
            out = work / "patched" / resource; out.parent.mkdir(parents=True, exist_ok=True)
            fonted = work / "fonted" / resource; fonted.parent.mkdir(parents=True, exist_ok=True)
            font_ids = font_ids_for_replacement(root / resource)
            replaced = work / "replaced" / resource; replaced.parent.mkdir(parents=True, exist_ok=True)
            run_font_replace(args.java, args.ffdec_jar, root / resource, replaced, args.font, font_ids)
            patch_font_names(replaced, fonted, font_ids, args.font_name)
            run_ffdec(args.java, args.ffdec_jar, fonted, out, text_folder)
            if out.read_bytes()[:3] not in {b"FWS", b"CWS", b"ZWS"}: raise RuntimeError(f"Invalid SWF from FFDec: {resource}")
        if dynamic:
            out = work / "patched" / MAIN_SWF; out.parent.mkdir(parents=True, exist_ok=True); updated = patch_main(root / MAIN_SWF, out, dynamic)
            if updated != sum(map(len, dynamic.values())): raise RuntimeError("ABC update count mismatch")
        for resource in sorted(files):
            source = root / resource; patched = work / "patched" / resource; backup = args.backup_dir / resource; backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, backup); shutil.copy2(patched, source); manifest["files"].append({"resource": resource, "backup_sha256": sha256(backup), "patched_sha256": sha256(source)}); print(f"[patched] {resource}")
    (args.backup_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[done] Backup: {args.backup_dir}"); return 0

if __name__ == "__main__":
    try: raise SystemExit(main())
    except Exception as error: print(f"ERROR: {error}", file=sys.stderr); raise
