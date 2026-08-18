# Сборка установщика

`dist/Detective-Grimoire-RU-Installer.exe` — готовый автономный установщик для GOG-версии Detective Grimoire 1.2.0. Он не содержит игровых ресурсов или английского текста: при запуске читает файлы только из локально установленной игры, сохраняет резервную копию и применяет перевод.

## Пересборка после изменения перевода

Нужны Windows, .NET SDK 10, Python 3.13, распакованная Java 21, папка JPEXS Free Flash Decompiler с `ffdec.jar`, чистая установленная GOG-версия игры 1.2.0 и её локальная резервная копия, созданная до русификации.

Из этой папки выполните:

```powershell
.\build_rusifier.ps1 `
  -GameDir "D:\Games\Detective Grimoire" `
  -VanillaBackup "D:\Backups\DetectiveGrimoire-GOG-1.2.0" `
  -JavaSource "D:\Tools\jdk-21-jre" `
  -FfdecSource "D:\Tools\ffdec" `
  -PythonRoot "C:\Python313"
```

Скрипт создаёт временную копию игры, вычисляет контрольные суммы оригинала и результата, а затем удаляет временную копию. В Git добавляйте только `dist/Detective-Grimoire-RU-Installer.exe`, исходники `RusifierApp`, `build_rusifier.ps1` и `sanitize_workbook.py`; папки `payload` и `payload-slim.zip` — локальные промежуточные файлы и игнорируются.
