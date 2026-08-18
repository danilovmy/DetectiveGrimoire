using System.Diagnostics;
using System.IO.Compression;
using System.Reflection;
using System.Security.Cryptography;
using System.Text.Json;

namespace GrimoireRusifier;

internal sealed record FileSignature(string Resource, string OriginalSha256, string PatchedSha256);
internal sealed record VersionManifest(string GameExecutable, List<FileSignature> Files);

internal static class Program
{
    [STAThread]
    private static void Main()
    {
        ApplicationConfiguration.Initialize();
        var gameRoot = AppContext.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar);
        string? archive = null;
        try
        {
            var payloadRoot = ExtractPayload();
            var manifest = JsonSerializer.Deserialize<VersionManifest>(File.ReadAllText(Path.Combine(payloadRoot, "version.json")))
                ?? throw new InvalidDataException("Не удалось прочитать manifest русификатора.");

            if (!File.Exists(Path.Combine(gameRoot, manifest.GameExecutable)))
            {
                Show("Перевод не применён", "Запустите этот EXE из папки установленной игры Detective Grimoire.", MessageBoxIcon.Warning);
                return;
            }

            var state = DetectState(gameRoot, manifest);
            if (state == InstallState.Patched)
            {
                Show("Перевод уже применён", "Установленная игра уже содержит эту версию перевода.", MessageBoxIcon.Information);
                return;
            }
            if (state != InstallState.Original)
            {
                Show("Не та версия игры", "Файлы игры не совпадают с поддерживаемой версией GOG 1.2.0 либо уже были изменены. Ничего не менялось.", MessageBoxIcon.Warning);
                return;
            }

            archive = CreateBackupArchive(gameRoot, manifest);
            RunPatch(gameRoot, payloadRoot, archive);

            if (DetectState(gameRoot, manifest) != InstallState.Patched)
                throw new InvalidOperationException("Проверка после применения не пройдена.");

            Show("Перевод применён", $"Оригинальные файлы сохранены в:\n{archive}\n\nДля отмены: переустановите игру или скопируйте файлы из этого архива обратно в папку игры.", MessageBoxIcon.Information);
        }
        catch (Exception error)
        {
            var restored = archive is not null && RestoreBackup(gameRoot, archive);
            var recovery = restored ? "\n\nОригиналы автоматически восстановлены из созданного архива." : "";
            Show("Перевод не применён", $"Ничего не считается установленным. Причина:\n{error.Message}{recovery}", MessageBoxIcon.Error);
        }
    }

    private static void RunPatch(string gameRoot, string payloadRoot, string archive)
    {
        var python = Path.Combine(payloadRoot, "python", "python.exe");
        var java = Path.Combine(payloadRoot, "jre", "bin", "java.exe");
        var ffdec = Path.Combine(payloadRoot, "ffdec", "ffdec.jar");
        var scripts = Path.Combine(payloadRoot, "scripts");
        var xlsx = Path.Combine(payloadRoot, "translations_ru.xlsx");
        var font = Path.Combine(payloadRoot, "comic.ttf");
        var timestamp = DateTime.Now.ToString("yyyyMMdd-HHmmss");
        var generatedCatalog = Path.Combine(gameRoot, "localization", "catalog");
        var applyBackup = Path.Combine(gameRoot, "localization", "backups", timestamp + "-ru-apply");
        var scaleBackup = Path.Combine(gameRoot, "localization", "backups", timestamp + "-ru-scale");

        Run(python, Quote(Path.Combine(scripts, "extract_texts.py")) + " --root " + Quote(gameRoot) + " --java " + Quote(java) + " --ffdec-jar " + Quote(ffdec), payloadRoot);
        Run(python, Quote(Path.Combine(scripts, "apply_translation.py")) + " --root " + Quote(gameRoot) + " --translations " + Quote(xlsx) + " --catalog " + Quote(Path.Combine(generatedCatalog, "occurrences.jsonl")) + " --java " + Quote(java) + " --ffdec-jar " + Quote(ffdec) + " --font " + Quote(font) + " --font-name " + Quote("Comic Sans MS") + " --backup-dir " + Quote(applyBackup), payloadRoot);
        Run(python, Quote(Path.Combine(scripts, "scale_text.py")) + " --root " + Quote(gameRoot) + " --manifest " + Quote(Path.Combine(applyBackup, "manifest.json")) + " --backup-dir " + Quote(scaleBackup) + " --scale 0.5", payloadRoot);
    }

    private static string CreateBackupArchive(string gameRoot, VersionManifest manifest)
    {
        var folder = Path.Combine(gameRoot, "localization", "backups");
        Directory.CreateDirectory(folder);
        var archive = Path.Combine(folder, $"Detective-Grimoire-originals-before-RU-{DateTime.Now:yyyyMMdd-HHmmss}.zip");
        using var output = ZipFile.Open(archive, ZipArchiveMode.Create);
        foreach (var item in manifest.Files)
            output.CreateEntryFromFile(Path.Combine(gameRoot, item.Resource), item.Resource, CompressionLevel.Optimal);
        return archive;
    }

    private static bool RestoreBackup(string gameRoot, string archive)
    {
        try
        {
            using var input = ZipFile.OpenRead(archive);
            foreach (var entry in input.Entries)
            {
                if (string.IsNullOrEmpty(entry.Name)) continue;
                var destination = Path.GetFullPath(Path.Combine(gameRoot, entry.FullName));
                if (!destination.StartsWith(gameRoot + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
                    throw new InvalidDataException("Некорректный путь в архиве резервной копии.");
                Directory.CreateDirectory(Path.GetDirectoryName(destination)!);
                entry.ExtractToFile(destination, true);
            }
            return true;
        }
        catch { return false; }
    }

    private static InstallState DetectState(string root, VersionManifest manifest)
    {
        var original = true;
        var patched = true;
        foreach (var item in manifest.Files)
        {
            var path = Path.Combine(root, item.Resource);
            if (!File.Exists(path)) return InstallState.Unsupported;
            var hash = HashFile(path);
            original &= hash.Equals(item.OriginalSha256, StringComparison.OrdinalIgnoreCase);
            patched &= hash.Equals(item.PatchedSha256, StringComparison.OrdinalIgnoreCase);
        }
        return patched ? InstallState.Patched : original ? InstallState.Original : InstallState.Unsupported;
    }

    private static string ExtractPayload()
    {
        var root = Path.Combine(Path.GetTempPath(), "DetectiveGrimoire-RU-Installer", "1.0.27");
        var marker = Path.Combine(root, ".ready");
        if (File.Exists(marker)) return root;
        if (Directory.Exists(root)) Directory.Delete(root, true);
        Directory.CreateDirectory(root);
        using var stream = Assembly.GetExecutingAssembly().GetManifestResourceStream("payload.zip")
            ?? throw new InvalidOperationException("Встроенные файлы русификатора не найдены.");
        using var archive = new ZipArchive(stream, ZipArchiveMode.Read);
        archive.ExtractToDirectory(root);
        File.WriteAllText(marker, "ok");
        return root;
    }

    private static void Run(string fileName, string arguments, string workingDirectory)
    {
        var process = Process.Start(new ProcessStartInfo(fileName, arguments)
        {
            WorkingDirectory = workingDirectory,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardError = true,
            RedirectStandardOutput = true,
        }) ?? throw new InvalidOperationException("Не удалось запустить встроенный инструмент.");
        var stdout = process.StandardOutput.ReadToEnd();
        var stderr = process.StandardError.ReadToEnd();
        process.WaitForExit();
        if (process.ExitCode != 0)
            throw new InvalidOperationException((stderr + "\n" + stdout).Trim());
    }

    private static string HashFile(string path)
    {
        using var file = File.OpenRead(path);
        return Convert.ToHexString(SHA256.HashData(file)).ToLowerInvariant();
    }

    private static string Quote(string value) => "\"" + value.Replace("\"", "\\\"") + "\"";
    private static void Show(string title, string text, MessageBoxIcon icon) => MessageBox.Show(text, title, MessageBoxButtons.OK, icon);
    private enum InstallState { Original, Patched, Unsupported }
}
