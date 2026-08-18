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
    private static Label? progressLabel;
    private static System.Windows.Forms.Timer? progressTimer;
    private static DateTime progressStartedAt;
    private static string progressStage = "Подготовка";
    private static string progressDetail = "Подготовка встроенных инструментов";

    [STAThread]
    private static void Main()
    {
        ApplicationConfiguration.Initialize();
        using var singleInstance = new Mutex(true, "DetectiveGrimoire-RU-Installer", out var isFirstInstance);
        if (!isFirstInstance)
        {
            Show("Перевод уже выполняется", "Установщик уже запущен. Дождитесь его итогового сообщения и не запускайте EXE повторно.", MessageBoxIcon.Information);
            return;
        }
        var gameRoot = AppContext.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar);
        var logPath = Path.Combine(gameRoot, "DetectiveGrimoire-RU-Installer.log");
        string? archive = null;
        Form? progress = null;
        try
        {
            Log(logPath, "Запуск установщика.");
            var payloadRoot = ExtractPayload();
            var manifest = JsonSerializer.Deserialize<VersionManifest>(File.ReadAllText(Path.Combine(payloadRoot, "version.json")), new JsonSerializerOptions { PropertyNameCaseInsensitive = true })
                ?? throw new InvalidDataException("Не удалось прочитать manifest русификатора.");
            Log(logPath, "Полезная нагрузка распакована, manifest прочитан.");

            if (!File.Exists(Path.Combine(gameRoot, manifest.GameExecutable)))
            {
                Show("Перевод не применён", "Запустите этот EXE из папки установленной игры Detective Grimoire.", MessageBoxIcon.Warning);
                return;
            }

            var state = DetectState(gameRoot, manifest);
            Log(logPath, $"Состояние файлов игры: {state}.");
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

            progress = ShowProgress();
            archive = CreateBackupArchive(gameRoot, manifest);
            Log(logPath, $"Резервная копия создана: {archive}");
            RunPatch(gameRoot, payloadRoot, archive, logPath);

            if (DetectState(gameRoot, manifest) != InstallState.Patched)
                throw new InvalidOperationException("Проверка после применения не пройдена.");

            CloseProgress(progress); progress = null;
            Log(logPath, "Перевод применён и проверен.");
            Show("Перевод применён", $"Русификация успешно применена.\n\nОригинальные файлы сохранены в:\n{archive}\n\nДля отмены: переустановите игру или скопируйте файлы из этого архива обратно в папку игры.\n\nСпасибо за перевод danilovmy и codex-terra.", MessageBoxIcon.Information);
        }
        catch (Exception error)
        {
            CloseProgress(progress); progress = null;
            Log(logPath, error.ToString());
            var restored = archive is not null && RestoreBackup(gameRoot, archive);
            var recovery = restored ? "\n\nОригиналы автоматически восстановлены из созданного архива." : "";
            Show("Перевод не применён", $"Ничего не считается установленным. Причина:\n{error.Message}{recovery}", MessageBoxIcon.Error);
        }
        finally { CloseProgress(progress); }
    }

    private static void RunPatch(string gameRoot, string payloadRoot, string archive, string logPath)
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

        SetProgressStage("Извлечение каталога строк", "Запускается анализ игровых ресурсов");
        Log(logPath, "Извлечение каталога строк.");
        Run(python, "-u " + Quote(Path.Combine(scripts, "extract_texts.py")) + " --root " + Quote(gameRoot) + " --java " + Quote(java) + " --ffdec-jar " + Quote(ffdec), payloadRoot, HandleExtractProgress);
        SetProgressStage("Подготовка применения перевода", "Запускается проверка каталога и таблицы перевода");
        Log(logPath, "Применение перевода.");
        Run(python, "-u " + Quote(Path.Combine(scripts, "apply_translation.py")) + " --root " + Quote(gameRoot) + " --translations " + Quote(xlsx) + " --catalog " + Quote(Path.Combine(generatedCatalog, "occurrences.jsonl")) + " --java " + Quote(java) + " --ffdec-jar " + Quote(ffdec) + " --font " + Quote(font) + " --font-name " + Quote("Comic Sans MS") + " --backup-dir " + Quote(applyBackup), payloadRoot, HandleApplyProgress);
        SetProgressStage("Настройка размера текста", "Подготавливается подгонка переведённых текстовых блоков");
        Log(logPath, "Масштабирование текста.");
        Run(python, "-u " + Quote(Path.Combine(scripts, "scale_text.py")) + " --root " + Quote(gameRoot) + " --manifest " + Quote(applyBackup + "\\manifest.json") + " --backup-dir " + Quote(scaleBackup) + " --scale 0.5", payloadRoot, HandleScaleProgress);
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
        // Меняем суффикс при изменении встроенной полезной нагрузки: иначе Windows
        // может повторно использовать распакованный набор файлов от прежней сборки.
        var payloadVersion = (Assembly.GetExecutingAssembly().GetName().Version?.ToString() ?? "current") + "-r2";
        var root = Path.Combine(Path.GetTempPath(), "DetectiveGrimoire-RU-Installer", payloadVersion);
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

    private static Form ShowProgress()
    {
        var form = new Form
        {
            Text = "Русификация Detective Grimoire",
            ClientSize = new Size(560, 140),
            FormBorderStyle = FormBorderStyle.FixedDialog,
            MaximizeBox = false,
            MinimizeBox = false,
            ControlBox = false,
            StartPosition = FormStartPosition.CenterScreen,
            TopMost = true,
            ShowInTaskbar = true,
        };
        progressStartedAt = DateTime.Now;
        progressStage = "Подготовка";
        progressDetail = "Создаётся резервная копия оригинальных файлов";
        progressLabel = new Label
        {
            Text = "",
            AutoSize = false,
            TextAlign = ContentAlignment.MiddleCenter,
            Dock = DockStyle.Fill,
            Font = SystemFonts.MessageBoxFont,
        };
        form.Controls.Add(progressLabel);
        progressTimer = new System.Windows.Forms.Timer { Interval = 1000 };
        progressTimer.Tick += (_, _) => UpdateProgressLabel();
        progressTimer.Start();
        UpdateProgressLabel();
        form.Show();
        Application.DoEvents();
        return form;
    }

    private static void CloseProgress(Form? form)
    {
        if (form is null || form.IsDisposed) return;
        progressTimer?.Stop();
        progressTimer?.Dispose();
        progressTimer = null;
        progressLabel = null;
        form.Close();
        form.Dispose();
    }

    private static void SetProgressStage(string stage, string detail)
    {
        progressStage = stage;
        progressDetail = detail;
        UpdateProgressLabel();
    }

    private static void HandleExtractProgress(string line)
    {
        if (line.StartsWith("[extract-plan] ", StringComparison.Ordinal))
            progressDetail = "Подготавливается список игровых ресурсов: " + line[15..];
        else if (line.StartsWith("[extract] ", StringComparison.Ordinal))
            progressDetail = "Извлекаются строки: группа " + line[10..];
        else if (line.StartsWith("[done] ", StringComparison.Ordinal) && line.Contains("SWF text occurrences", StringComparison.Ordinal))
            progressDetail = "Извлечено текстовых вхождений: " + line[7..];
    }

    private static void HandleApplyProgress(string line)
    {
        if (line.StartsWith("[prepare] ", StringComparison.Ordinal))
            progressDetail = "Подготовка: " + line[10..];
        else if (line.StartsWith("[plan] ", StringComparison.Ordinal))
            progressDetail = "План: " + line[7..];
        else if (line.StartsWith("[applying] ", StringComparison.Ordinal))
            progressDetail = "Обрабатывается SWF: " + line[11..];
        else if (line.StartsWith("[patched] ", StringComparison.Ordinal))
            progressDetail = "Файл применён: " + line[10..];
    }

    private static void HandleScaleProgress(string line)
    {
        if (line.StartsWith("[scaled] ", StringComparison.Ordinal))
            progressDetail = "Подгонка текста: " + line[9..];
    }

    private static void UpdateProgressLabel()
    {
        if (progressLabel is null || progressLabel.IsDisposed) return;
        var elapsed = DateTime.Now - progressStartedAt;
        progressLabel.Text = $"{progressStage}…\n{progressDetail}\nПрошло: {elapsed:mm\\:ss}\n\nНе закрывайте это окно и дождитесь результата.";
    }

    private static void Log(string path, string message)
    {
        File.AppendAllText(path, $"[{DateTime.Now:yyyy-MM-dd HH:mm:ss}] {message}{Environment.NewLine}");
    }

    private static void Run(string fileName, string arguments, string workingDirectory, Action<string>? onOutput = null)
    {
        var process = Process.Start(new ProcessStartInfo(fileName, arguments)
        {
            WorkingDirectory = workingDirectory,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardError = true,
            RedirectStandardOutput = true,
        }) ?? throw new InvalidOperationException("Не удалось запустить встроенный инструмент.");
        var stdout = new System.Text.StringBuilder();
        var stderr = new System.Text.StringBuilder();
        process.OutputDataReceived += (_, eventArgs) =>
        {
            if (eventArgs.Data is null) return;
            stdout.AppendLine(eventArgs.Data);
            onOutput?.Invoke(eventArgs.Data);
        };
        process.ErrorDataReceived += (_, eventArgs) =>
        {
            if (eventArgs.Data is not null) stderr.AppendLine(eventArgs.Data);
        };
        process.BeginOutputReadLine();
        process.BeginErrorReadLine();
        while (!process.WaitForExit(100)) Application.DoEvents();
        if (process.ExitCode != 0)
            throw new InvalidOperationException((stderr.ToString() + "\n" + stdout).Trim());
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
