#!/usr/bin/env python3
"""Extract Detective Grimoire text into a translation catalog.

Static Flash text is exported through FFDec. Dynamic strings are discovered in
decompiled game-specific ActionScript and tied back to their exact AVM2 string
pool indices. The original game files are read-only inputs.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import lzma
import re
import struct
import subprocess
import sys
import tempfile
import zlib
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


RECORD_SEPARATOR = "\n--- RECORDSEPARATOR ---\n"
AS_STRING_RE = re.compile(r'"(?:\\.|[^"\\])*"')
LETTER_RE = re.compile(r"[A-Za-z]")
WORD_RE = re.compile(r"[A-Za-z]{2,}")
ROOT_SCRIPT_NAMES = {"DGGameDsk.as", "DGGameDskVanilla.as", "DGAchievements.as"}
GAME_SCRIPT_ROOTS = {"audio", "data", "gfx", "minds", "screens"}


@dataclass(frozen=True)
class AbcString:
    abc_name: str
    index: int
    value: str


@dataclass(frozen=True)
class AsContext:
    path: str
    line: int
    source_line: str

    @property
    def short(self) -> str:
        return f"{self.path}:{self.line}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--java", required=True, type=Path)
    parser.add_argument("--ffdec-jar", required=True, type=Path)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Game directory (defaults to the parent of localization/)",
    )
    return parser.parse_args()


def run_ffdec(java: Path, ffdec_jar: Path, arguments: list[str]) -> None:
    command = [str(java), "-jar", str(ffdec_jar), *arguments]
    completed = subprocess.run(
        command,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if completed.returncode:
        tail = "\n".join(completed.stdout.splitlines()[-80:])
        raise RuntimeError(f"FFDec failed ({completed.returncode}):\n{tail}")


def relative_posix(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def read_plain_text_export(path: Path) -> list[str]:
    raw = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    if raw.endswith("\n"):
        raw = raw[:-1]
    return raw.split(RECORD_SEPARATOR)


def translation_key(source: str) -> str:
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:20]
    return f"src_{digest}"


def translation_action(source: str) -> str:
    if WORD_RE.search(source):
        return "translate"
    if LETTER_RE.search(source):
        return "review"
    return "keep"


def export_static_occurrences(
    root: Path,
    asset_root: Path,
    work_root: Path,
    java: Path,
    ffdec_jar: Path,
) -> list[dict]:
    export_root = work_root / "static"
    swf_files = sorted(asset_root.rglob("*.swf"))
    parent_dirs = sorted({path.parent for path in swf_files})

    for parent in parent_dirs:
        relative_parent = parent.relative_to(asset_root)
        output = export_root / relative_parent
        output.mkdir(parents=True, exist_ok=True)
        print(f"[static] {relative_parent.as_posix() or '.'}")
        run_ffdec(
            java,
            ffdec_jar,
            [
                "-format",
                "text:plain",
                "-onerror",
                "abort",
                "-export",
                "text",
                str(output),
                str(parent),
            ],
        )

    occurrences: list[dict] = []
    for swf in swf_files:
        relative_parent = swf.parent.relative_to(asset_root)
        swf_export = export_root / relative_parent / swf.name
        if not swf_export.exists():
            continue
        for text_file in sorted(swf_export.glob("*.txt"), key=lambda p: int(p.stem)):
            segments = read_plain_text_export(text_file)
            source = "".join(segments)
            resource = relative_posix(swf, root)
            tag_id = int(text_file.stem)
            occurrences.append(
                {
                    "id": f"swf:{resource}#text:{tag_id}",
                    "kind": "swf_text",
                    "resource": resource,
                    "tag_id": tag_id,
                    "source": source,
                    "segments": segments,
                    "segment_count": len(segments),
                    "translation_key": translation_key(source),
                }
            )
    return occurrences


def decode_as_string(token: str) -> str:
    body = token[1:-1]

    def replace_escape(match: re.Match[str]) -> str:
        escape = match.group(1)
        simple = {
            "n": "\n",
            "r": "\r",
            "t": "\t",
            "b": "\b",
            "f": "\f",
            "\\": "\\",
            '"': '"',
            "'": "'",
        }
        if escape in simple:
            return simple[escape]
        if escape.startswith("u") and len(escape) == 5:
            return chr(int(escape[1:], 16))
        if escape.startswith("x") and len(escape) == 3:
            return chr(int(escape[1:], 16))
        return escape

    return re.sub(r"\\(u[0-9A-Fa-f]{4}|x[0-9A-Fa-f]{2}|.)", replace_escape, body)


def is_game_script(path: Path, scripts_root: Path) -> bool:
    relative = path.relative_to(scripts_root)
    if len(relative.parts) == 1:
        return relative.name in ROOT_SCRIPT_NAMES
    if relative.parts[0] in GAME_SCRIPT_ROOTS:
        return True
    return relative.parts[:3] == ("com", "sfbgames", "grimoire")


def collect_actionscript_contexts(scripts_root: Path) -> dict[str, list[AsContext]]:
    contexts: dict[str, list[AsContext]] = defaultdict(list)
    for path in sorted(scripts_root.rglob("*.as")):
        if not is_game_script(path, scripts_root):
            continue
        relative = path.relative_to(scripts_root).as_posix()
        for line_number, line in enumerate(path.read_text("utf-8-sig").splitlines(), 1):
            for match in AS_STRING_RE.finditer(line):
                value = decode_as_string(match.group(0))
                context = AsContext(relative, line_number, line.strip())
                if context not in contexts[value]:
                    contexts[value].append(context)
    return contexts


def read_u30(buffer: bytes, position: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 35, 7):
        byte = buffer[position]
        position += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, position
    raise ValueError("Invalid U30 value")


def decompress_swf(path: Path) -> bytes:
    raw = path.read_bytes()
    signature = raw[:3]
    if signature == b"FWS":
        return raw
    if signature == b"CWS":
        return b"FWS" + raw[3:8] + zlib.decompress(raw[8:])
    if signature == b"ZWS":
        properties = raw[12:17]
        property_byte = properties[0]
        lc = property_byte % 9
        quotient = property_byte // 9
        lp = quotient % 5
        pb = quotient // 5
        filters = [
            {
                "id": lzma.FILTER_LZMA1,
                "dict_size": int.from_bytes(properties[1:5], "little"),
                "lc": lc,
                "lp": lp,
                "pb": pb,
            }
        ]
        body = lzma.decompress(raw[17:], format=lzma.FORMAT_RAW, filters=filters)
        return b"FWS" + raw[3:8] + body
    raise ValueError(f"Unsupported SWF signature: {signature!r}")


def iter_swf_tags(buffer: bytes) -> Iterable[tuple[int, bytes]]:
    position = 8
    rect_bits = buffer[position] >> 3
    position += (5 + rect_bits * 4 + 7) // 8
    position += 4  # frame rate and frame count
    while position + 2 <= len(buffer):
        header = int.from_bytes(buffer[position : position + 2], "little")
        position += 2
        tag_code = header >> 6
        length = header & 0x3F
        if length == 0x3F:
            length = int.from_bytes(buffer[position : position + 4], "little")
            position += 4
        payload = buffer[position : position + length]
        yield tag_code, payload
        position += length
        if tag_code == 0:
            break


def parse_abc_strings(abc_name: str, abc: bytes) -> list[AbcString]:
    position = 4  # minor_version, major_version
    for width in (None, None, 8):  # int, uint, double pools
        count, position = read_u30(abc, position)
        for _ in range(max(0, count - 1)):
            if width is None:
                _, position = read_u30(abc, position)
            else:
                position += width

    count, position = read_u30(abc, position)
    strings: list[AbcString] = []
    for index in range(1, count):
        length, position = read_u30(abc, position)
        value = abc[position : position + length].decode("utf-8", "replace")
        position += length
        strings.append(AbcString(abc_name, index, value))
    return strings


def extract_abc_strings(main_swf: Path) -> list[AbcString]:
    strings: list[AbcString] = []
    for tag_code, payload in iter_swf_tags(decompress_swf(main_swf)):
        if tag_code != 82:  # DoABC
            continue
        name_end = payload.index(0, 4)
        abc_name = payload[4:name_end].decode("utf-8", "replace") or "unnamed"
        strings.extend(parse_abc_strings(abc_name, payload[name_end + 1 :]))
    return strings


def is_dynamic_translation_context(value: str, context: AsContext) -> bool:
    line = context.source_line
    if "joiner1Text =" in line or "joiner2Text =" in line:
        return True
    if "new DialogueBox(" in line:
        return bool(WORD_RE.search(value) and " " in value)
    return False


def looks_technical(value: str, contexts: list[AsContext]) -> bool:
    if not LETTER_RE.search(value):
        return True
    if re.search(r"[\\/]", value) or re.search(
        r"\.(swf|mp3|png|json|as)$", value, re.I
    ):
        return True
    if re.fullmatch(r"[\w$]+(?:\.[\w$]+)+(?:[:][\w$]+)?", value):
        return True
    if re.fullmatch(r"[a-z][A-Za-z0-9_]*", value) and " " not in value:
        return True
    technical_markers = (
        "addExamineAudio(",
        "addPath(",
        "addCharacters(",
        "setScreen(",
        "getMP3Path(",
        "addChar(",
        "addClue(",
        "addCatagory(",
        "gotoAndStop(",
    )
    return any(marker in context.source_line for context in contexts for marker in technical_markers)


def export_code_data(
    root: Path,
    main_swf: Path,
    work_root: Path,
    java: Path,
    ffdec_jar: Path,
) -> tuple[list[dict], list[dict], int]:
    decompiled_root = work_root / "decompiled"
    decompiled_root.mkdir(parents=True, exist_ok=True)
    print("[code] decompiling ActionScript for context")
    run_ffdec(
        java,
        ffdec_jar,
        [
            "-config",
            "parallelSpeedUp=true",
            "-onerror",
            "abort",
            "-timeout",
            "60",
            "-exportTimeout",
            "900",
            "-export",
            "script",
            str(decompiled_root),
            str(main_swf),
        ],
    )
    scripts_root = decompiled_root / "scripts"
    contexts_by_value = collect_actionscript_contexts(scripts_root)
    abc_strings = extract_abc_strings(main_swf)
    abc_by_value: dict[str, list[AbcString]] = defaultdict(list)
    for item in abc_strings:
        abc_by_value[item.value].append(item)

    selected_values = {
        value
        for value, contexts in contexts_by_value.items()
        if LETTER_RE.search(value)
        and any(is_dynamic_translation_context(value, context) for context in contexts)
    }

    occurrences: list[dict] = []
    for value in sorted(selected_values):
        relevant_contexts = [
            context
            for context in contexts_by_value[value]
            if is_dynamic_translation_context(value, context)
        ]
        for item in abc_by_value.get(value, []):
            resource = relative_posix(main_swf, root)
            occurrences.append(
                {
                    "id": f"abc:{resource}#{item.abc_name}:{item.index}",
                    "kind": "abc_string",
                    "resource": resource,
                    "abc_name": item.abc_name,
                    "abc_string_index": item.index,
                    "source": value,
                    "contexts": [context.short for context in relevant_contexts],
                    "translation_key": translation_key(value),
                }
            )

    missing = sorted(value for value in selected_values if value not in abc_by_value)
    if missing:
        raise RuntimeError(f"Selected strings missing from ABC pool: {missing!r}")

    audit_rows: list[dict] = []
    for value, contexts in contexts_by_value.items():
        indices = abc_by_value.get(value, [])
        if value in selected_values:
            classification = "translate"
            reason = "dynamic player-facing text"
        elif looks_technical(value, contexts):
            classification = "technical"
            reason = "path, identifier, audio key, or engine linkage"
        else:
            classification = "review"
            reason = "game-specific string; not proven player-facing"
        audit_rows.append(
            {
                "classification": classification,
                "source": value,
                "abc_indices": ",".join(
                    f"{item.abc_name}:{item.index}" for item in indices
                ),
                "contexts": " | ".join(context.short for context in contexts),
                "reason": reason,
            }
        )
    order = {"translate": 0, "review": 1, "technical": 2}
    audit_rows.sort(key=lambda row: (order[row["classification"]], row["source"].casefold()))
    return occurrences, audit_rows, len(abc_strings)


def build_translation_rows(occurrences: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for occurrence in occurrences:
        grouped[occurrence["translation_key"]].append(occurrence)

    rows: list[dict] = []
    for key, items in grouped.items():
        source = items[0]["source"]
        kinds = sorted({item["kind"] for item in items})
        locations = [item["id"] for item in items]
        segment_counts = sorted(
            {item["segment_count"] for item in items if "segment_count" in item}
        )
        comments: list[str] = []
        if "abc_string" in kinds:
            comments.append("dynamic text in the main SWF")
        if "<font" in source or "<br" in source:
            comments.append("preserve HTML tags")
        if segment_counts and max(segment_counts) > 1:
            comments.append("source uses styled text segments")
        rows.append(
            {
                "key": key,
                "action": translation_action(source),
                "status": "todo" if LETTER_RE.search(source) else "keep",
                "source": source,
                "translation": "",
                "occurrences": len(items),
                "kinds": ",".join(kinds),
                "segment_counts": ",".join(map(str, segment_counts)),
                "locations": " | ".join(locations[:8]),
                "comment": "; ".join(comments),
            }
        )
    action_order = {"translate": 0, "review": 1, "keep": 2}
    rows.sort(key=lambda row: (action_order[row["action"]], row["source"].casefold()))
    return rows


def write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def write_inventory(
    path: Path,
    root: Path,
    asset_root: Path,
    static_occurrences: list[dict],
    code_occurrences: list[dict],
    translation_rows: list[dict],
    code_audit: list[dict],
    abc_string_count: int,
) -> None:
    unique_static = len({item["source"] for item in static_occurrences})
    static_characters = sum(len(item["source"]) for item in static_occurrences)
    static_words = sum(len(item["source"].split()) for item in static_occurrences)
    categories = Counter(Path(item["resource"]).parts[-2] for item in static_occurrences)
    actions = Counter(row["action"] for row in translation_rows)
    code_classes = Counter(row["classification"] for row in code_audit)
    all_swfs = list(root.rglob("*.swf"))
    raster_files = [
        file
        for extension in ("*.png", "*.jpg", "*.jpeg", "*.bmp", "*.gif")
        for file in root.rglob(extension)
    ]
    non_icon_rasters = [file for file in raster_files if "icons" not in file.parts]

    category_lines = "\n".join(
        f"| `{category}` | {count} |" for category, count in sorted(categories.items())
    )
    content = f"""# Инвентаризация текста Detective Grimoire

Экстрактор читает исходные игровые ресурсы, но не изменяет их.

## Итог

- SWF-файлов в игре: **{len(all_swfs)}** (основной SWF и дочерние ресурсы).
- Текстовых тегов в дочерних SWF: **{len(static_occurrences)}**.
- Уникальных строк в текстовых тегах: **{unique_static}**.
- Объём статического текста: **{static_characters}** знаков, примерно **{static_words}** слов.
- Динамических переводимых строк в ActionScript: **{len(code_occurrences)}** вхождений.
- Строк в переводческом каталоге без дублей: **{len(translation_rows)}**.
- В исходном ABC-пуле основного SWF: **{abc_string_count}** строковых констант; все игровые кандидаты перечислены в `code_audit.csv`.

## Текстовые теги по группам ресурсов

| Группа | Вхождения |
|---|---:|
{category_lines}

## Статусы в `translations_ru.csv`

- `translate`: {actions.get('translate', 0)} строк с обычным английским текстом.
- `review`: {actions.get('review', 0)} коротких или неоднозначных строк.
- `keep`: {actions.get('keep', 0)} числовых, пустых или чисто пунктуационных значений.

## Аудит строк кода

- доказанно отображаемые динамические строки: {code_classes.get('translate', 0)};
- требуют ручной проверки: {code_classes.get('review', 0)};
- технические ключи, пути и идентификаторы: {code_classes.get('technical', 0)}.

## Изображения

На диске найдено {len(raster_files)} растровых файлов; вне папки иконок — {len(non_icon_rasters)}. Внутри SWF могут быть встроенные изображения и векторные надписи — их визуальный аудит оставлен на следующий этап.
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    main_swf = root / "DetectiveGrimoireDesktopVanilla.swf"
    asset_root = root / "assets" / "swf-dsk"
    catalog_root = root / "localization" / "catalog"
    report_path = root / "localization" / "reports" / "inventory.md"

    for required in (args.java, args.ffdec_jar, main_swf, asset_root):
        if not required.exists():
            raise FileNotFoundError(required)

    with tempfile.TemporaryDirectory(prefix="grimoire-text-extract-") as temporary:
        work_root = Path(temporary)
        static_occurrences = export_static_occurrences(
            root, asset_root, work_root, args.java, args.ffdec_jar
        )
        code_occurrences, code_audit, abc_string_count = export_code_data(
            root, main_swf, work_root, args.java, args.ffdec_jar
        )

    occurrences = sorted(
        [*static_occurrences, *code_occurrences], key=lambda item: item["id"]
    )
    translation_rows = build_translation_rows(occurrences)

    write_jsonl(catalog_root / "occurrences.jsonl", occurrences)
    write_csv(
        catalog_root / "translations_ru.csv",
        [
            "key",
            "action",
            "status",
            "source",
            "translation",
            "occurrences",
            "kinds",
            "segment_counts",
            "locations",
            "comment",
        ],
        translation_rows,
    )
    write_csv(
        catalog_root / "code_audit.csv",
        ["classification", "source", "abc_indices", "contexts", "reason"],
        code_audit,
    )
    write_inventory(
        report_path,
        root,
        asset_root,
        static_occurrences,
        code_occurrences,
        translation_rows,
        code_audit,
        abc_string_count,
    )

    print(f"[done] {len(static_occurrences)} SWF text occurrences")
    print(f"[done] {len(code_occurrences)} dynamic code occurrences")
    print(f"[done] {len(translation_rows)} unique catalog rows")
    print(f"[done] {catalog_root / 'translations_ru.csv'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise
