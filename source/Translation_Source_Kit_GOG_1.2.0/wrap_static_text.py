#!/usr/bin/env python3
"""Wrap safe single-style Flash text fields at word boundaries."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import shutil
from collections import defaultdict
from pathlib import Path

import apply_translation as swf


TEXT_TAGS = {11, 33}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--translations", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path, required=True)
    return parser.parse_args()


def read_bits(data: bytes | bytearray, bit_pos: int, count: int) -> tuple[int, int]:
    value = 0
    for _ in range(count):
        value = (value << 1) | ((data[bit_pos // 8] >> (7 - bit_pos % 8)) & 1)
        bit_pos += 1
    return value, bit_pos


def read_signed_bits(data: bytes | bytearray, bit_pos: int, count: int) -> tuple[int, int]:
    value, bit_pos = read_bits(data, bit_pos, count)
    if value & (1 << (count - 1)):
        value -= 1 << count
    return value, bit_pos


def skip_rect(data: bytes | bytearray, pos: int) -> int:
    bits, _ = read_bits(data, pos * 8, 5)
    return pos + (5 + bits * 4 + 7) // 8


def rect(data: bytes | bytearray, pos: int) -> tuple[tuple[int, int, int, int], int]:
    bit_pos = pos * 8
    bits, bit_pos = read_bits(data, bit_pos, 5)
    values = []
    for _ in range(4):
        value, bit_pos = read_signed_bits(data, bit_pos, bits)
        values.append(value)
    return (values[0], values[1], values[2], values[3]), (bit_pos + 7) // 8


def skip_matrix(data: bytes | bytearray, pos: int) -> int:
    bit_pos = pos * 8
    has_scale, bit_pos = read_bits(data, bit_pos, 1)
    if has_scale:
        bits, bit_pos = read_bits(data, bit_pos, 5)
        bit_pos += bits * 2
    has_rotate, bit_pos = read_bits(data, bit_pos, 1)
    if has_rotate:
        bits, bit_pos = read_bits(data, bit_pos, 5)
        bit_pos += bits * 2
    bits, bit_pos = read_bits(data, bit_pos, 5)
    bit_pos += bits * 2
    return (bit_pos + 7) // 8


def encode_glyphs(glyphs: list[tuple[int, int]], glyph_bits: int, advance_bits: int) -> bytes:
    output = bytearray((len(glyphs) * (glyph_bits + advance_bits) + 7) // 8)
    bit_pos = 0
    for glyph, advance in glyphs:
        encoded = (glyph << advance_bits) | (advance & ((1 << advance_bits) - 1))
        for shift in range(glyph_bits + advance_bits - 1, -1, -1):
            if encoded & (1 << shift):
                output[bit_pos // 8] |= 1 << (7 - bit_pos % 8)
            bit_pos += 1
    return bytes(output)


def parse_records(payload: bytes, code: int) -> dict | None:
    bounds, pos = rect(payload, 2)
    pos = skip_matrix(payload, pos)
    glyph_bits, advance_bits = payload[pos], payload[pos + 1]
    pos += 2
    body_start = pos
    font_id = color = height = None
    x = y = 0
    base_x = base_y = None
    units = []
    while pos < len(payload) and payload[pos] != 0:
        flags = payload[pos]
        pos += 1
        if not flags & 0x80:
            return None
        has_font, has_color = bool(flags & 0x08), bool(flags & 0x04)
        has_y, has_x = bool(flags & 0x02), bool(flags & 0x01)
        if has_font:
            font_id = int.from_bytes(payload[pos:pos + 2], "little")
            pos += 2
        if has_color:
            size = 4 if code == 33 else 3
            color = payload[pos:pos + size]
            pos += size
        if has_x:
            x = int.from_bytes(payload[pos:pos + 2], "little", signed=True)
            pos += 2
        if has_y:
            y = int.from_bytes(payload[pos:pos + 2], "little", signed=True)
            pos += 2
        if has_font:
            height = int.from_bytes(payload[pos:pos + 2], "little")
            pos += 2
        if font_id is None or color is None or height is None:
            return None
        if base_x is None:
            base_x, base_y = x, y
        count = payload[pos]
        pos += 1
        bit_pos = pos * 8
        for _ in range(count):
            glyph, bit_pos = read_bits(payload, bit_pos, glyph_bits)
            advance, bit_pos = read_signed_bits(payload, bit_pos, advance_bits)
            units.append((glyph, advance, (font_id, color, height)))
        pos = (bit_pos + 7) // 8
    if pos >= len(payload) or payload[pos] != 0:
        return None
    return {
        "bounds": bounds, "glyph_bits": glyph_bits, "advance_bits": advance_bits,
        "units": units, "x": base_x, "y": base_y, "body_start": body_start, "body_end": pos + 1,
    }


def fold_words(text: str, units: list[tuple[int, int, tuple[int, bytes, int]]], width: int) -> list[list[tuple[int, int, tuple[int, bytes, int]]]] | None:
    if len(text) != len(units) or "\n" in text or "\r" in text:
        return None
    lines: list[list[tuple[int, int]]] = []
    start = 0
    while start < len(units):
        total, last_space, index = 0, None, start
        while index < len(units):
            total += max(0, units[index][1])
            if text[index].isspace():
                last_space = index
            if total > width:
                if last_space is None or last_space < start:
                    return None
                end = last_space
                if end == start:
                    start += 1
                    break
                lines.append(units[start:end])
                start = last_space + 1
                break
            index += 1
        else:
            lines.append(units[start:])
            break
    return lines if len(lines) > 1 else None


def wrap_payload(payload: bytes, code: int, text: str) -> bytes | None:
    record = parse_records(payload, code)
    if not record or not record["units"]:
        return None
    xmin, xmax, ymin, ymax = record["bounds"]
    lines = fold_words(text, record["units"], xmax - xmin)
    if not lines:
        return None
    pitch = max(1, round(max(unit[2][2] for unit in record["units"]) * 1.15))
    if len(lines) > max(1, (ymax - ymin) // pitch):
        return None
    encoded = bytearray()
    for number, line in enumerate(lines):
        x = record["x"]
        offset = 0
        while offset < len(line):
            style = line[offset][2]
            end = offset + 1
            while end < len(line) and line[end][2] == style:
                end += 1
            glyphs = [(glyph, advance) for glyph, advance, _style in line[offset:end]]
            font_id, color, height = style
            encoded += b"\x8f" + font_id.to_bytes(2, "little") + color
            encoded += x.to_bytes(2, "little", signed=True)
            encoded += (record["y"] + number * pitch).to_bytes(2, "little", signed=True)
            encoded += height.to_bytes(2, "little") + bytes([len(glyphs)])
            encoded += encode_glyphs(glyphs, record["glyph_bits"], record["advance_bits"])
            x += sum(advance for _glyph, advance in glyphs)
            offset = end
    encoded += b"\x00"
    return payload[:record["body_start"]] + encoded + payload[record["body_end"]:]


def transform_swf(path: Path, requested: dict[int, str]) -> tuple[bytes, int]:
    raw = path.read_bytes()
    fws, properties = swf.decompress_swf(raw)
    rebuilt = bytearray(fws[:swf.tag_start(fws)])
    changed = 0
    for code, _length, payload in swf.iter_tags(fws):
        replacement = payload
        char_id = int.from_bytes(payload[:2], "little") if code in TEXT_TAGS and len(payload) >= 2 else None
        if char_id in requested:
            candidate = wrap_payload(payload, code, requested[char_id])
            if candidate is not None:
                replacement = candidate
                changed += 1
        header = (code << 6) | (len(replacement) if len(replacement) < 63 else 63)
        rebuilt += header.to_bytes(2, "little")
        if len(replacement) >= 63:
            rebuilt += len(replacement).to_bytes(4, "little")
        rebuilt += replacement
    return swf.compress_swf(bytes(rebuilt), raw[:3], properties), changed


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    args = parse_args()
    translations = swf.load_translations(args.translations)
    occurrences = [json.loads(line) for line in args.catalog.read_text(encoding="utf-8").splitlines() if line]
    requested: dict[str, dict[int, str]] = defaultdict(dict)
    for item in occurrences:
        text = translations.get(item["translation_key"])
        if item["kind"] == "swf_text" and text:
            requested[item["resource"]][item["tag_id"]] = text
    allowed = {item["resource"] for item in json.loads(args.manifest.read_text(encoding="utf-8"))["files"]}
    args.backup_dir.mkdir(parents=True, exist_ok=False)
    report = {"created_at": dt.datetime.now(dt.timezone.utc).isoformat(), "files": []}
    for resource in sorted(allowed & requested.keys()):
        target = args.root / resource
        replacement, count = transform_swf(target, requested[resource])
        if not count:
            continue
        backup = args.backup_dir / resource
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, backup)
        target.write_bytes(replacement)
        swf.decompress_swf(target.read_bytes())
        report["files"].append({"resource": resource, "wrapped_tags": count, "backup_sha256": sha256(backup), "patched_sha256": sha256(target)})
        print(f"[wrapped] {resource}: {count}")
    (args.backup_dir / "manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[done] Wrapped {sum(item['wrapped_tags'] for item in report['files'])} text fields")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
