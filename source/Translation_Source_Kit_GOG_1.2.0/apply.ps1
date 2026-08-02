[CmdletBinding()]
param([string]$GameDir, [string]$Translations, [string]$Python = "python", [string]$Font = "C:\Windows\Fonts\comic.ttf", [string]$FontName = "Comic Sans MS", [ValidateRange(0.1, 1.0)][double]$FontScale = 0.5, [switch]$DryRun, [string]$ToolRoot = (Join-Path $env:TEMP "grimoire-localization-tools"))
$ErrorActionPreference = "Stop"
$GameRoot = (Resolve-Path $(if ($GameDir) { $GameDir } else { Join-Path $PSScriptRoot ".." })).Path
if (-not $Translations) { $Translations = Get-ChildItem -LiteralPath (Join-Path $GameRoot "localization\outputs") -Recurse -Filter "translations_ru.xlsx" | Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName }
if (-not $Translations) { throw "Translation XLSX was not found. Pass -Translations <path>." }
$Translations = (Resolve-Path $Translations).Path; $ffdecVersion = "26.2.0"; $ffdecZip = Join-Path $ToolRoot "ffdec.zip"; $ffdecDir = Join-Path $ToolRoot "ffdec"; $jreZip = Join-Path $ToolRoot "jre.zip"; $jreDir = Join-Path $ToolRoot "jre"
New-Item -ItemType Directory -Force -Path $ToolRoot | Out-Null
if (-not (Test-Path $ffdecZip)) { Invoke-WebRequest -Uri "https://github.com/jindrapetrik/jpexs-decompiler/releases/download/version$ffdecVersion/ffdec_$ffdecVersion.zip" -OutFile $ffdecZip }
if (-not (Test-Path $ffdecDir)) { Expand-Archive -LiteralPath $ffdecZip -DestinationPath $ffdecDir }
if (-not (Test-Path $jreZip)) { Invoke-WebRequest -Uri "https://api.adoptium.net/v3/binary/latest/21/ga/windows/x64/jre/hotspot/normal/eclipse" -OutFile $jreZip }
if (-not (Test-Path $jreDir)) { Expand-Archive -LiteralPath $jreZip -DestinationPath $jreDir }
$java = Get-ChildItem -LiteralPath $jreDir -Recurse -Filter java.exe | Select-Object -First 1 -ExpandProperty FullName; $jar = Get-ChildItem -LiteralPath $ffdecDir -Recurse -Filter ffdec.jar | Select-Object -First 1 -ExpandProperty FullName
if (-not $java -or -not $jar) { throw "FFDec or Java setup failed." }
$extractor = Join-Path $PSScriptRoot "extract_texts.py"
& $Python $extractor --root $GameRoot --java $java --ffdec-jar $jar
if ($LASTEXITCODE -ne 0) { throw "Local text extraction failed with exit code $LASTEXITCODE." }
$Catalog = Join-Path $GameRoot "localization\catalog\occurrences.jsonl"
$Catalog = (Resolve-Path $Catalog).Path
$backup = Join-Path $GameRoot ("localization\backups\" + (Get-Date -Format "yyyyMMdd-HHmmss")); $params = @((Join-Path $PSScriptRoot "apply_translation.py"), "--root", $GameRoot, "--translations", $Translations, "--catalog", $Catalog, "--java", $java, "--ffdec-jar", $jar, "--font", $Font, "--font-name", $FontName, "--backup-dir", $backup)
if ($DryRun) { $params += "--dry-run" }; & $Python @params
if ($LASTEXITCODE -ne 0) { throw "Localization apply failed with exit code $LASTEXITCODE." }
if (-not $DryRun -and $FontScale -ne 1.0) {
    $scaleBackup = Join-Path $GameRoot ("localization\backups\" + (Get-Date -Format "yyyyMMdd-HHmmss") + "-font" + [int]($FontScale * 100))
    & $Python (Join-Path $PSScriptRoot "scale_text.py") --root $GameRoot --manifest (Join-Path $backup "manifest.json") --backup-dir $scaleBackup --scale $FontScale
    if ($LASTEXITCODE -ne 0) { throw "Font scaling failed with exit code $LASTEXITCODE." }
}
