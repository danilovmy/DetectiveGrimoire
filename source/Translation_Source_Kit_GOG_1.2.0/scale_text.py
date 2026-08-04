#!/usr/bin/env python3
"""Scale embedded SWF text heights without changing translated strings or fonts."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import shutil
import sys
from pathlib import Path

import apply_translation as swf


TEXT_TAGS = {11, 33, 37}  # DefineText, DefineText2, DefineEditText


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True, help="Manifest from a successful translation run")
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--scale", type=float, default=0.75)
    return parser.parse_args()


def read_bits(data: bytes | bytearray, bit_pos: int, count: int) -> tuple[int, int]:
    value = 0
    for _ in range(count):
        value = (value << 1) | ((data[bit_pos // 8] >> (7 - bit_pos % 8)) & 1)
        bit_pos += 1
    return value, bit_pos


def skip_rect(data: bytes | bytearray, pos: int) -> int:
    bits, _ = read_bits(data, pos * 8, 5)
    return pos + (5 + bits * 4 + 7) // 8


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


def scaled(value: int, factor: float) -> int:
    return max(1, min(65535, round(value * factor)))


def read_signed_bits(data: bytes | bytearray, bit_pos: int, count: int) -> tuple[int, int]:
    value = 0
    for _ in range(count):
        value = (value << 1) | ((data[bit_pos // 8] >> (7 - bit_pos % 8)) & 1)
        bit_pos += 1
    if value & (1 << (count - 1)):
        value -= 1 << count
    return value, bit_pos


def write_signed_bits(data: bytearray, bit_pos: int, count: int, value: int) -> None:
    lower, upper = -(1 << (count - 1)), (1 << (count - 1)) - 1
    value = max(lower, min(upper, value)) & ((1 << count) - 1)
    for shift in range(count - 1, -1, -1):
        byte_index, offset = divmod(bit_pos, 8)
        mask = 1 << (7 - offset)
        data[byte_index] = (data[byte_index] | mask) if value & (1 << shift) else (data[byte_index] & ~mask)
        bit_pos += 1


def scale_static_text(payload: bytes, code: int, factor: float) -> tuple[bytes, int]:
    data = bytearray(payload)
    pos = skip_matrix(data, skip_rect(data, 2))
    glyph_bits, advance_bits = data[pos], data[pos + 1]
    pos += 2
    changed = 0
    while pos < len(data):
        flags = data[pos]
        pos += 1
        if flags == 0:
            break
        if not flags & 0x80:
            raise ValueError("Malformed TEXTRECORD")
        has_font = bool(flags & 0x08)
        has_color = bool(flags & 0x04)
        has_y_offset = bool(flags & 0x02)
        has_x_offset = bool(flags & 0x01)
        if has_font:
            pos += 2  # font ID
        if has_color:
            pos += 4 if code == 33 else 3
        if has_x_offset:
            pos += 2
        if has_y_offset:
            pos += 2
        if has_font:
            old = int.from_bytes(data[pos:pos + 2], "little")
            new = scaled(old, factor)
            data[pos:pos + 2] = new.to_bytes(2, "little")
            changed += old != new
            pos += 2
        glyph_count = data[pos]
        pos += 1
        bit_pos = pos * 8
        for _ in range(glyph_count):
            bit_pos += glyph_bits
            advance_pos = bit_pos
            advance, bit_pos = read_signed_bits(data, bit_pos, advance_bits)
            new_advance = round(advance * factor)
            write_signed_bits(data, advance_pos, advance_bits, new_advance)
            changed += advance != new_advance
        pos = (bit_pos + 7) // 8
    return bytes(data), changed


def scale_edit_text(payload: bytes, factor: float) -> tuple[bytes, int]:
    data = bytearray(payload)
    pos = skip_rect(data, 2)
    flags1, flags2 = data[pos], data[pos + 1]
    pos += 2
    has_font = bool(flags1 & 0x01)
    has_font_class = bool(flags2 & 0x80)
    if has_font:
        pos += 2
    if has_font_class:
        pos = data.index(0, pos) + 1
    if not (has_font or has_font_class):
        return bytes(data), 0
    old = int.from_bytes(data[pos:pos + 2], "little")
    new = scaled(old, factor)
    data[pos:pos + 2] = new.to_bytes(2, "little")
    return bytes(data), int(old != new)


def fitting_scale(source: Path, target: Path, tag_id: int, final_scale: float) -> float:
    capacity = swf.text_record_advances(source, tag_id)
    actual = swf.text_record_advances(target, tag_id)
    if not capacity or len(capacity) != len(actual):
        return 1.0
    ratios = [limit / (used * final_scale) for limit, used in zip(capacity, actual) if used * final_scale > limit]
    return max(0.1, min(1.0, min(ratios) * 0.98)) if ratios else 1.0


def scale_swf(path: Path, factor: float, tag_scales: dict[str, float] | None = None) -> int:
    raw = path.read_bytes()
    fws, lzma_properties = swf.decompress_swf(raw)
    data = bytearray(fws)
    pos = swf.tag_start(data)
    changed = 0
    while pos + 2 <= len(data):
        header = int.from_bytes(data[pos:pos + 2], "little")
        pos += 2
        code, length = header >> 6, header & 63
        if length == 63:
            length = int.from_bytes(data[pos:pos + 4], "little")
            pos += 4
        end = pos + length
        if code in TEXT_TAGS:
            payload = bytes(data[pos:end])
            if code in {11, 33}:
                tag_id = str(int.from_bytes(payload[:2], "little"))
                local_factor = (tag_scales or {}).get(tag_id, 1.0)
                replacement, count = scale_static_text(payload, code, factor * local_factor)
            else:
                replacement, count = scale_edit_text(payload, factor)
            if len(replacement) != len(payload):
                raise ValueError("Text-height pass must not change a tag length")
            data[pos:end] = replacement
            changed += count
        pos = end
        if code == 0:
            break
    if changed:
        path.write_bytes(swf.compress_swf(bytes(data), raw[:3], lzma_properties))
    return changed


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    args = parse_args()
    if not 0 < args.scale <= 1:
        raise ValueError("--scale must be greater than 0 and not exceed 1")
    root = args.root.resolve()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    files = [item["resource"] for item in manifest["files"]]
    args.backup_dir.mkdir(parents=True, exist_ok=False)
    output = {"created_at": dt.datetime.now(dt.timezone.utc).isoformat(), "scale": args.scale, "source_manifest": str(args.manifest), "files": []}
    translated_tags = manifest.get("static_tags", {})
    for resource in files:
        target = root / resource
        if not target.exists():
            raise FileNotFoundError(target)
        backup = args.backup_dir / resource
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, backup)
        source = args.manifest.parent / resource
        local_scales = {tag: scale for tag in translated_tags.get(resource, []) if (scale := fitting_scale(source, target, int(tag), args.scale)) < 0.995}
        count = scale_swf(target, args.scale, local_scales)
        # A second parse verifies that recompression produced a valid SWF.
        swf.decompress_swf(target.read_bytes())
        output["files"].append({"resource": resource, "heights_scaled": count, "fitted_tags": local_scales, "backup_sha256": sha256(backup), "patched_sha256": sha256(target)})
        suffix = f"; {len(local_scales)} fitted text tags" if local_scales else ""
        print(f"[scaled] {resource}: {count} text-height values{suffix}")
    (args.backup_dir / "manifest.json").write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[done] Backup: {args.backup_dir}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise
