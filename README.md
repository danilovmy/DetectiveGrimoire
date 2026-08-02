# Русификация Detective Grimoire

Неофициальный набор для локальной сборки русификации **GOG-версии Detective Grimoire 1.2.0**.

> Пожалуйста, купите игру — она действительно этого заслуживает.

- GOG game ID: `2087170042`
- GOG build ID: `55081706531997481`

## Купить игру

- [Steam — Detective Grimoire](https://store.steampowered.com/app/272600/Detective_Grimoire/)
- [GOG — Detective Grimoire](https://www.gog.com/en/game/detective_grimoire)

## Что есть в репозитории

В репозитории находятся переводческая таблица и скрипты, которые работают с уже установленной у пользователя игрой. Исходные литературные тексты игры — диалоги, реплики и описания — в репозитории не содержатся. Поэтому колонка `Оригинал` в `translations_ru.xlsx` намеренно пуста. При запуске скрипт:

1. читает ресурсы из указанной папки игры;
2. создаёт техническую карту текстовых тегов только локально;
3. применяет русский перевод и сохраняет резервные копии исходных файлов рядом с игрой.

Игровые SWF, графика, звук, исполняемые файлы и готовые изменённые ресурсы в репозитории не хранятся и не распространяются.

## Как заполнить XLSX оригиналами локально

Нужна установленная игра. В PowerShell из папки `source/Translation_Source_Kit_GOG_1.2.0` выполните сначала безопасное извлечение без изменения файлов игры:

```powershell
.\apply.ps1 -GameDir "D:\Games\Detective Grimoire" -Translations .\translations_ru.xlsx -DryRun
```

Затем создайте отдельную таблицу с оригиналами:

```powershell
python .\fill_originals.py --workbook .\translations_ru.xlsx --catalog "D:\Games\Detective Grimoire\localization\catalog\occurrences.jsonl" --output .\translations_ru_with_originals.xlsx
```

Файл `translations_ru_with_originals.xlsx` создаётся только на компьютере пользователя и игнорируется Git.

## Применение

Нужны Windows, Python 3.10+ и установленная GOG-версия игры 1.2.0. Откройте PowerShell в папке `source/Translation_Source_Kit_GOG_1.2.0` и выполните:

```powershell
.\apply.ps1 -GameDir "D:\Games\Detective Grimoire" -Translations .\translations_ru.xlsx
```

При первом запуске сценарий скачает Java 21 и JPEXS Free Flash Decompiler в локальный временный кэш. Полная инструкция — в [README набора](source/Translation_Source_Kit_GOG_1.2.0/README.md).

## Благодарности

См. [CREDITS.md](CREDITS.md).
