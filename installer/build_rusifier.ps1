[CmdletBinding()]
param(
    [string]$Translations = "",
    [string]$VanillaBackup = "",
    [string]$GameDir = "",
    [string]$JavaSource = "",
    [string]$FfdecSource = "",
    [string]$PythonRoot = "",
    [switch]$UseCurrentPatchedGame
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$repoRoot = Split-Path $root -Parent
$payload = Join-Path $root 'payload'
$project = Join-Path $root 'RusifierApp\RusifierApp.csproj'
$toolkit = Join-Path $repoRoot 'source\Translation_Source_Kit_GOG_1.2.0'
if (-not $Translations) { $Translations = Join-Path $toolkit 'translations_ru.xlsx' }
if (-not $GameDir) { throw 'Specify -GameDir with a clean installed GOG 1.2.0 game folder.' }
if (-not $VanillaBackup) { throw 'Specify -VanillaBackup with a local backup manifest of the clean supported game.' }
if (-not $JavaSource) { throw 'Specify -JavaSource with an unpacked Java 21 runtime.' }
if (-not $FfdecSource) { throw 'Specify -FfdecSource with a folder containing ffdec.jar.' }
if (-not $PythonRoot) { throw 'Specify -PythonRoot with a Python 3.13 installation folder.' }
$javaSource = $JavaSource
$ffdecSource = $FfdecSource
$pythonRoot = $PythonRoot

foreach ($path in @($Translations, $VanillaBackup, $GameDir, $javaSource, $ffdecSource, $toolkit, $pythonRoot)) {
    if (-not (Test-Path -LiteralPath $path)) { throw "Required path does not exist: $path" }
}

Remove-Item -LiteralPath $payload -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $payload, (Join-Path $payload 'scripts') | Out-Null

# The runtime extracts its own catalog from the player's original game. No English game prose is embedded.
Copy-Item -LiteralPath (Join-Path $toolkit 'extract_texts.py'), (Join-Path $toolkit 'apply_translation.py'), (Join-Path $toolkit 'scale_text.py') -Destination (Join-Path $payload 'scripts')
Copy-Item -LiteralPath (Join-Path $env:WINDIR 'Fonts\comic.ttf') -Destination (Join-Path $payload 'comic.ttf')
Copy-Item -LiteralPath $javaSource -Destination (Join-Path $payload 'jre') -Recurse
Copy-Item -LiteralPath $ffdecSource -Destination (Join-Path $payload 'ffdec') -Recurse

# Minimal self-contained Python 3.13 runtime: standard library only, no third-party packages.
New-Item -ItemType Directory -Path (Join-Path $payload 'python') | Out-Null
Copy-Item -LiteralPath (Join-Path $pythonRoot 'python.exe'), (Join-Path $pythonRoot 'python313.dll'), (Join-Path $pythonRoot 'python3.dll'), (Join-Path $pythonRoot 'vcruntime140.dll'), (Join-Path $pythonRoot 'vcruntime140_1.dll') -Destination (Join-Path $payload 'python')
Copy-Item -LiteralPath (Join-Path $pythonRoot 'DLLs') -Destination (Join-Path $payload 'python\DLLs') -Recurse
& robocopy (Join-Path $pythonRoot 'Lib') (Join-Path $payload 'python\Lib') /E /XD site-packages test tkinter idlelib ensurepip turtledemo venv curses dbm /XF '*.pyc' | Out-Null
if ($LASTEXITCODE -gt 7) { throw 'Could not copy the embedded Python standard library.' }

& (Join-Path $pythonRoot 'python.exe') (Join-Path $root 'sanitize_workbook.py') $Translations (Join-Path $payload 'translations_ru.xlsx')

# Recreate the exact supported original game in a temporary copy, then calculate the hashes after this workbook is applied.
if ($UseCurrentPatchedGame) {
    Write-Warning 'Using the supplied already-patched game for result hashes. Use only after a full reference build has succeeded.'
    $scratchGame = $GameDir
} else {
    $scratch = Join-Path ([System.IO.Path]::GetTempPath()) ('grimoire-ru-build-' + [guid]::NewGuid())
    Copy-Item -LiteralPath $GameDir -Destination $scratch -Recurse
    $scratchGame = Join-Path $scratch (Split-Path $GameDir -Leaf)
    Get-Content -LiteralPath (Join-Path $VanillaBackup 'manifest.json') -Raw | ConvertFrom-Json | ForEach-Object { $_.files } | ForEach-Object {
        $from = Join-Path $VanillaBackup $_.resource
        $to = Join-Path $scratchGame $_.resource
        New-Item -ItemType Directory -Force -Path (Split-Path $to -Parent) | Out-Null
        Copy-Item -LiteralPath $from -Destination $to -Force
    }
}

try {
    if (-not $UseCurrentPatchedGame) {
    $python = Join-Path $payload 'python\python.exe'; $java = Join-Path $payload 'jre\bin\java.exe'; $ffdec = Join-Path $payload 'ffdec\ffdec.jar'
    & $python (Join-Path $payload 'scripts\extract_texts.py') --root $scratchGame --java $java --ffdec-jar $ffdec
    if ($LASTEXITCODE -ne 0) { throw 'Failed to build the catalog from the reference game.' }
    $applyBackup = Join-Path $scratchGame 'localization\build-apply'
    & $python (Join-Path $payload 'scripts\apply_translation.py') --root $scratchGame --translations (Join-Path $payload 'translations_ru.xlsx') --catalog (Join-Path $scratchGame 'localization\catalog\occurrences.jsonl') --java $java --ffdec-jar $ffdec --font (Join-Path $payload 'comic.ttf') --font-name 'Comic Sans MS' --backup-dir $applyBackup
    if ($LASTEXITCODE -ne 0) { throw 'Translation application to the reference game failed. Check the workbook edits.' }
    & $python (Join-Path $payload 'scripts\scale_text.py') --root $scratchGame --manifest (Join-Path $applyBackup 'manifest.json') --backup-dir (Join-Path $scratchGame 'localization\build-scale') --scale 0.5
    if ($LASTEXITCODE -ne 0) { throw 'Text scaling on the reference game failed.' }
    }

    $entries = Get-Content -LiteralPath (Join-Path $VanillaBackup 'manifest.json') -Raw | ConvertFrom-Json | ForEach-Object { $_.files }
    $files = foreach ($entry in $entries) {
        [ordered]@{ resource = $entry.resource; originalSha256 = $entry.backup_sha256; patchedSha256 = (Get-FileHash -LiteralPath (Join-Path $scratchGame $entry.resource) -Algorithm SHA256).Hash.ToLowerInvariant() }
    }
    [ordered]@{ gameExecutable = 'Detective Grimoire.exe'; files = @($files) } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $payload 'version.json') -Encoding utf8
}
finally {
    if (-not $UseCurrentPatchedGame) { Remove-Item -LiteralPath $scratch -Recurse -Force -ErrorAction SilentlyContinue }
}

$zip = Join-Path $root 'payload-slim.zip'
Remove-Item -LiteralPath $zip -Force -ErrorAction SilentlyContinue
Compress-Archive -Path (Join-Path $payload '*') -DestinationPath $zip -CompressionLevel Optimal

dotnet publish $project -c Release -r win-x64 --self-contained true -p:PublishSingleFile=true -p:DebugType=none
$output = Join-Path $root 'dist\Detective-Grimoire-RU-Installer.exe'
Copy-Item -LiteralPath (Join-Path $root 'RusifierApp\bin\Release\net10.0-windows\win-x64\publish\RusifierApp.exe') -Destination $output -Force
Write-Host "Built: $output"
