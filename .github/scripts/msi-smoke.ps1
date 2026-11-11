# Дымовая проверка MSI (user-logon-autostart, задача 6.3): установка,
# работа, обновление, откат и удаление на раннере Windows по шагам 1–14
# раздела «Тесты без Windows и проверка на Windows в CI» design.md.
#
# Раннер работает от администратора без интерактивного входа, поэтому
# вход пользователя заменяет запуск exe без аргументов и без консоли
# (Start-Exe: родитель — cmd с DETACHED_PROCESS, как Explorer без консоли).
#
# Параметры:
#   -Msi1, -Build1          MSI версии сборки и подписанная папка, из
#                           которой он собран
#   -Msi2, -Build2          MSI версии на единицу больше из копии папки
#                           с другой версией exe и DLL сообщений в ресурсах
#   -Msi2Rebuilt            тот же второй MSI, собранный заново (другой
#                           ProductCode): поверх второго должен отказать
#   -BuildDir               папка сборки из архива (dist/FilePickerAPI):
#                           для run_test.bat (шаг 14)
#   -FilesDirectory         UNC-путь филиала из branches.toml
#   -AppVersion             app._version.version: в /openapi.json и 1000
#   -FileVersion1, -FileVersion2  FILEVERSION exe в первом и втором MSI
#   -LogDir                 куда сложить журналы msiexec
#
# Версия приложения у обоих MSI одна (вшита при компиляции), поэтому
# MSI различаются по SHA-256 и FILEVERSION файлов, а не по ответу API.
#
# Используется в build-exe.yml после сборки MSI. Скрипт PowerShell 7
# (shell: pwsh), как и sign.ps1 и startup-smoke.ps1. При любой ошибке
# экземпляры останавливаются, продукт удаляется, джоб падает.

param(
  [Parameter(Mandatory)][string]$Msi1,
  [Parameter(Mandatory)][string]$Msi2,
  [Parameter(Mandatory)][string]$Msi2Rebuilt,
  [Parameter(Mandatory)][string]$Build1,
  [Parameter(Mandatory)][string]$Build2,
  [Parameter(Mandatory)][string]$BuildDir,
  [Parameter(Mandatory)][string]$FilesDirectory,
  [Parameter(Mandatory)][string]$AppVersion,
  [Parameter(Mandatory)][string]$FileVersion1,
  [Parameter(Mandatory)][string]$FileVersion2,
  [Parameter(Mandatory)][string]$LogDir
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'smoke-common.ps1')
$Msi1, $Msi2, $Msi2Rebuilt, $Build1, $Build2, $BuildDir =
  $Msi1, $Msi2, $Msi2Rebuilt, $Build1, $Build2, $BuildDir | ForEach-Object { (Resolve-Path $_).Path }
New-Item -ItemType Directory -Force $LogDir | Out-Null
$LogDir = (Resolve-Path $LogDir).Path

$installDir = Join-Path $env:ProgramFiles 'FilePickerAPI'
$exe = Join-Path $installDir 'FilePickerAPI.exe'
$envFile = Join-Path $installDir '.env'
# Файл сообщений в папке сборки и под именем с версией пакета в папке
# установки: обновление добавляет файл, а не заменяет тот, что держит
# служба журнала событий (изменение msi-message-file-in-use). Файлы обеих
# версий постоянные, поэтому сверка знает оба и их источники
$buildMessageDll = 'FilePickerAPI.messages.dll'
$messageFiles = @{
  "FilePickerAPI.messages.$FileVersion1.dll" = Join-Path $Build1 $buildMessageDll
  "FilePickerAPI.messages.$FileVersion2.dll" = Join-Path $Build2 $buildMessageDll
}
$runKey = 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Run'
$sourceKey = 'HKLM:\SYSTEM\CurrentControlSet\Services\EventLog\Application\FilePickerAPI'
$serviceLog = Join-Path $env:LOCALAPPDATA 'FilePickerAPI\logs\FilePickerAPI.log'
$port = 8000
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$usersSid = '*S-1-5-32-545'
$taskName = 'FilePickerAPI MSI smoke'
$corsEnv = 'CORS_ORIGINS=https://example.com'
$script:msiCount = 0
$script:portHolder = $null

Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;

public static class SmokeWindows {
  delegate bool EnumProc(IntPtr hwnd, IntPtr lParam);

  [DllImport("user32.dll")]
  static extern bool EnumWindows(EnumProc callback, IntPtr lParam);

  [DllImport("user32.dll", CharSet = CharSet.Unicode)]
  static extern int GetClassName(IntPtr hwnd, StringBuilder name, int size);

  [DllImport("user32.dll")]
  static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);

  [DllImport("user32.dll", SetLastError = true)]
  static extern bool PostMessage(IntPtr hwnd, uint message, IntPtr wParam, IntPtr lParam);

  // Окна верхнего уровня (и скрытые) заданного класса у процесса
  public static IntPtr[] Find(string className, uint pid) {
    var found = new List<IntPtr>();
    EnumWindows((hwnd, lParam) => {
      uint owner;
      GetWindowThreadProcessId(hwnd, out owner);
      if (owner == pid) {
        var name = new StringBuilder(256);
        GetClassName(hwnd, name, name.Capacity);
        if (name.ToString() == className) found.Add(hwnd);
      }
      return true;
    }, IntPtr.Zero);
    return found.ToArray();
  }

  public static bool Close(IntPtr hwnd) {
    const uint WM_CLOSE = 0x0010;
    return PostMessage(hwnd, WM_CLOSE, IntPtr.Zero, IntPtr.Zero);
  }

  [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
  struct StartupInfo {
    public int cb;
    public string lpReserved, lpDesktop, lpTitle;
    public int dwX, dwY, dwXSize, dwYSize, dwXCountChars, dwYCountChars, dwFillAttribute, dwFlags;
    public short wShowWindow, cbReserved2;
    public IntPtr lpReserved2, hStdInput, hStdOutput, hStdError;
  }

  [StructLayout(LayoutKind.Sequential)]
  struct ProcessInformation {
    public IntPtr hProcess, hThread;
    public int dwProcessId, dwThreadId;
  }

  [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
  static extern bool CreateProcessW(string application, StringBuilder commandLine,
    IntPtr processAttributes, IntPtr threadAttributes, bool inheritHandles, uint flags,
    IntPtr environment, string currentDirectory, ref StartupInfo startupInfo,
    out ProcessInformation processInformation);

  [DllImport("kernel32.dll")]
  static extern bool CloseHandle(IntPtr handle);

  // Процесс без консоли (DETACHED_PROCESS) и без стандартных потоков,
  // как у Explorer: запущенный им exe не прицепится к консоли раннера, а
  // сервис не найдёт окна консоли («запуск без окна»). Без
  // STARTF_USESTDHANDLES с нулями cmd получил бы значения дескрипторов
  // pwsh и передал бы их exe
  public static Detached StartDetached(string application, string commandLine, string directory) {
    const uint DETACHED_PROCESS = 0x00000008, CREATE_NEW_PROCESS_GROUP = 0x00000200;
    const int STARTF_USESTDHANDLES = 0x00000100;
    var startup = new StartupInfo();
    startup.cb = Marshal.SizeOf(typeof(StartupInfo));
    startup.dwFlags = STARTF_USESTDHANDLES;
    ProcessInformation info;
    if (!CreateProcessW(application, new StringBuilder(commandLine), IntPtr.Zero, IntPtr.Zero,
        false, DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP, IntPtr.Zero, directory,
        ref startup, out info)) {
      throw new System.ComponentModel.Win32Exception();
    }
    CloseHandle(info.hThread);
    return new Detached(info.hProcess, info.dwProcessId);
  }
}

public sealed class Detached {
  [DllImport("kernel32.dll")]
  static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);

  [DllImport("kernel32.dll")]
  static extern bool GetExitCodeProcess(IntPtr handle, out uint code);

  readonly IntPtr handle;
  public readonly int Id;

  public Detached(IntPtr handle, int id) { this.handle = handle; Id = id; }

  public bool WaitForExit(int milliseconds) {
    return WaitForSingleObject(handle, (uint)milliseconds) == 0;
  }

  public int ExitCode {
    get { uint code; GetExitCodeProcess(handle, out code); return (int)code; }
  }
}

// Кого Restart Manager закрыл бы ради замены файлов: тот же опрос
// (RmRegisterResources и RmGetList), что делает msiexec в InstallValidate.
// Имён в журнале msiexec при /qn нет, только «Successfully shut down»
public static class SmokeRestartManager {
  [StructLayout(LayoutKind.Sequential)]
  struct UniqueProcess {
    public int dwProcessId;
    public System.Runtime.InteropServices.ComTypes.FILETIME ProcessStartTime;
  }

  [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
  struct ProcessInfo {
    public UniqueProcess Process;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)] public string strAppName;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 64)] public string strServiceShortName;
    public int ApplicationType;
    public uint AppStatus;
    public uint TSSessionId;
    [MarshalAs(UnmanagedType.Bool)] public bool bRestartable;
  }

  [DllImport("rstrtmgr.dll", CharSet = CharSet.Unicode)]
  static extern int RmStartSession(out uint session, int flags, StringBuilder key);

  [DllImport("rstrtmgr.dll")]
  static extern int RmEndSession(uint session);

  [DllImport("rstrtmgr.dll", CharSet = CharSet.Unicode)]
  static extern int RmRegisterResources(uint session, uint fileCount, string[] files,
    uint applicationCount, IntPtr applications, uint serviceCount, string[] services);

  [DllImport("rstrtmgr.dll")]
  static extern int RmGetList(uint session, out uint needed, ref uint count,
    [In, Out] ProcessInfo[] info, out uint rebootReasons);

  // Строки «PID 123 Windows Event Log: service EventLog (RmService, …)»
  public static string[] Holders(string[] files) {
    const int ERROR_MORE_DATA = 234;
    uint session;
    int error = RmStartSession(out session, 0, new StringBuilder(33));
    if (error != 0) throw new System.ComponentModel.Win32Exception(error);
    try {
      error = RmRegisterResources(session, (uint)files.Length, files, 0, IntPtr.Zero, 0, null);
      if (error != 0) throw new System.ComponentModel.Win32Exception(error);
      uint needed, count = 0, reasons;
      ProcessInfo[] info = new ProcessInfo[0];
      error = RmGetList(session, out needed, ref count, null, out reasons);
      while (error == ERROR_MORE_DATA) {
        info = new ProcessInfo[needed];
        count = needed;
        error = RmGetList(session, out needed, ref count, info, out reasons);
      }
      if (error != 0) throw new System.ComponentModel.Win32Exception(error);
      var result = new List<string>();
      string[] types = { "RmUnknownApp", "RmMainWindow", "RmOtherWindow", "RmService", "RmExplorer", "RmConsole" };
      for (int i = 0; i < count; i++) {
        var p = info[i];
        string type = p.ApplicationType >= 0 && p.ApplicationType < types.Length
          ? types[p.ApplicationType] : (p.ApplicationType == 1000 ? "RmCritical" : p.ApplicationType.ToString());
        result.Add(string.Format("PID {0} {1}{2} ({3}, restartable: {4})", p.Process.dwProcessId, p.strAppName,
          string.IsNullOrEmpty(p.strServiceShortName) ? "" : ": service " + p.strServiceShortName,
          type, p.bRestartable));
      }
      if (reasons != 0) result.Add(string.Format("reboot reasons: 0x{0:x}", reasons));
      return result.ToArray();
    } finally {
      RmEndSession(session);
    }
  }
}
'@

function Fail([string]$message) { throw $message }

function Pass([string]$message) { Write-Host "OK: $message" }

function Step([string]$name) { Write-Host "=== $name" }

function Wait-Until([int]$seconds, [scriptblock]$condition) {
  $deadline = (Get-Date).AddSeconds($seconds)
  do {
    $result = & $condition
    if ($result) { return $result }
    Start-Sleep -Milliseconds 500
  } while ((Get-Date) -lt $deadline)
  # По истечении срока ничего не выводит: @(Wait-Until …) — пустой массив
}

# --- msiexec ---------------------------------------------------------

# Кто держит файлы папки установки, по опросу Restart Manager. Только
# вывод: держатели — не ошибка (шаг 11 их и проверяет), а сбой опроса
# не должен ронять проверку. Служба журнала событий в списке ожидаема:
# она держит файл сообщений, прочитав тексты событий (шаги 4 и 5). Но
# msiexec этот файл не заменяет (у каждой версии свой, изменение
# msi-message-file-in-use), и закрывать службу ему незачем: это
# проверяет Assert-EventLogKept после каждого msiexec
function Show-FilesInUse([string]$what) {
  try {
    if (-not (Test-Path $installDir)) { return }
    $files = @(Get-ChildItem $installDir -Recurse -File -Force | ForEach-Object FullName)
    if (-not $files) { return }
    $holders = @([SmokeRestartManager]::Holders($files))
    if (-not $holders) { Write-Host "files in use ${what}: none"; return }
    Write-Host "files in use ${what}:"
    $holders | ForEach-Object { Write-Host "  $_" }
  } catch {
    Write-Host "files in use ${what}: Restart Manager query failed: $_"
  }
}

# Последние строки журнала msiexec, пока он пишется: журнал открыт
# msiexec на запись, поэтому чтение — с FileShare.ReadWrite
function Get-MsiLogTail([string]$log, [int]$count = 3) {
  try {
    $share = [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete
    $stream = [IO.FileStream]::new($log, 'Open', 'Read', $share)
    try {
      $reader = [IO.StreamReader]::new($stream, $true)  # журнал в UTF-16 с BOM
      return @($reader.ReadToEnd() -split "`r?`n" | Where-Object { $_.Trim() } | Select-Object -Last $count)
    } finally {
      $stream.Dispose()
    }
  } catch {
    return @("(log is not readable: $($_.Exception.Message))")
  }
}

# Без Start-Process -Wait: он ждёт и дочерние процессы, а Restart
# Manager запускает exe заново, и ожидание длилось бы, пока exe работает.
# Журнал с «!» сбрасывается построчно: при зависании в нём последнее
# действие, а не буфер. Пока msiexec работает, раз в минуту в вывод
# шага идёт хвост журнала: если раннер перестанет отвечать и журналы
# не выгрузятся (так было в прогоне 43027367514), видно, на чём встало
function Invoke-Msi([string]$action, [string]$package, [string]$label, [int]$seconds = 600) {
  $script:msiCount++
  $log = Join-Path $LogDir ('{0:d2}-{1}.log' -f $script:msiCount, $label)
  $script:lastMsiLog = $log
  Show-FilesInUse "before msiexec $action ($label)"
  $eventLog = Get-EventLogService
  $arguments = @($action, "`"$package`"", '/qn', '/norestart', '/l*v!', "`"$log`"")
  $started = Get-Date
  $process = Start-Process msiexec.exe -ArgumentList $arguments -PassThru
  $null = $process.Handle  # иначе ExitCode после выхода недоступен
  $deadline = $started.AddSeconds($seconds)
  while (-not $process.WaitForExit([int][Math]::Max(0, [Math]::Min(60000, ($deadline - (Get-Date)).TotalMilliseconds)))) {
    $elapsed = [int]((Get-Date) - $started).TotalSeconds
    if ((Get-Date) -ge $deadline) {
      Write-Host "::error::msiexec $action $label did not exit in $seconds s"
      Show-FilesInUse "after $elapsed s of msiexec $action ($label)"
      Show-MsiLog $log
      Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
      Fail "msiexec $action $label did not exit in $seconds s"
    }
    Write-Host "msiexec $action ($label) still running after $elapsed s, log tail:"
    Get-MsiLogTail $log | ForEach-Object { Write-Host "  $_" }
  }
  $elapsed = [Math]::Round(((Get-Date) - $started).TotalSeconds, 1)
  Write-Host "msiexec $action $(Split-Path -Leaf $package) ($label) -> $($process.ExitCode) in $elapsed s"
  # Закрывал ли Restart Manager приложения: кого — в «files in use» выше
  Get-Content $log -ErrorAction SilentlyContinue |
    Where-Object { $_ -match 'RESTART MANAGER: (Successfully|Failed|Did not|Previously)' } |
    ForEach-Object { $_ -replace '^MSI .*?\]: ', '' } | Select-Object -Unique |
    ForEach-Object { Write-Host "  $_" }
  Assert-EventLogKept $eventLog "msiexec $action $label"
  return $process.ExitCode
}

# Служба журнала событий держит файл сообщений, прочитав тексты событий.
# Если msiexec заменил бы его, Restart Manager остановил бы EventLog и
# зависимые службы и запустил снова, и PID службы сменился бы
function Get-EventLogService {
  return Get-CimInstance Win32_Service -Filter "Name='EventLog'"
}

function Assert-EventLogKept($before, [string]$what) {
  $after = Get-EventLogService
  if ($after.State -ne 'Running' -or $after.ProcessId -ne $before.ProcessId) {
    Fail "${what}: EventLog was restarted (PID $($before.ProcessId) -> $($after.ProcessId), $($after.State))"
  }
}

# Строки Restart Manager о закрытии приложений. Имён в журнале при /qn
# нет, поэтому проверяется сам факт закрытия
function Get-RestartManagerShutdowns([string]$log) {
  return @(Get-Content $log -ErrorAction SilentlyContinue |
    Where-Object { $_ -match 'RESTART MANAGER: (Successfully shut down|Previously shut down)' })
}

# Причина отказа — в подробном журнале msiexec: строки перед первым
# «Return value 3» (действие, которое упало), иначе хвост журнала
function Show-MsiLog([string]$log) {
  if (-not $log -or -not (Test-Path $log)) { return }
  $lines = @(Get-Content $log -ErrorAction SilentlyContinue)
  $failed = [Array]::FindIndex([string[]]$lines, [Predicate[string]] { param($l) $l -match 'Return value 3' })
  $range = if ($failed -ge 0) { $lines[[Math]::Max(0, $failed - 40)..[Math]::Min($lines.Count - 1, $failed + 5)] }
           else { $lines | Select-Object -Last 40 }
  Write-Host "--- $(Split-Path -Leaf $log)"
  $range | ForEach-Object { Write-Host $_ }
  # Сообщения об ошибках Windows Installer: «Error 1925.», «Note: 1: 2203»,
  # «Product: … -- Installation failed»
  $lines | Where-Object { $_ -match '\bError [0-9]{4}\b|Note: 1: [0-9]{4}|Product: .*(failed|Error)' } |
    Select-Object -First 20 | ForEach-Object { Write-Host "  $_" }
}

function Assert-MsiCode([int]$code, [int[]]$expected, [string]$what) {
  if ($code -notin $expected) {
    Show-MsiLog $script:lastMsiLog
    Fail "${what}: msiexec exited with $code, expected $($expected -join ' or ')"
  }
}

function Install-Msi([string]$package, [string]$label) {
  Assert-MsiCode (Invoke-Msi '/i' $package $label) @(0) "msiexec /i $label"
}

function Uninstall-Msi([string]$package, [string]$label) {
  Assert-MsiCode (Invoke-Msi '/x' $package $label) @(0) "msiexec /x $label"
}

# --- файлы установки -------------------------------------------------

# Расхождения папки установки с папкой сборки, из которой собран MSI:
# SHA-256 каждого файла, кроме bat-скриптов (их нет в пакете), и лишние
# файлы, кроме .env. Файл сообщений сборки ожидается под именем с версией
# её MSI ($version); файл сообщений другой версии — не лишний: он
# постоянный и должен совпадать с DLL сообщений своей сборки
function Compare-Install([string]$build, [string]$version) {
  $expected = @{}
  # -Force: скрытые файлы WiX тоже включает в пакет
  Get-ChildItem $build -Recurse -File -Force | Where-Object Extension -ne '.bat' | ForEach-Object {
    $relative = $_.FullName.Substring($build.Length + 1)
    if ($relative -eq $buildMessageDll) { $relative = "FilePickerAPI.messages.$version.dll" }
    $expected[$relative] = $_.FullName
  }
  $diff = foreach ($relative in $expected.Keys) {
    $target = Join-Path $installDir $relative
    if (-not (Test-Path -LiteralPath $target)) { "missing $relative"; continue }
    if ((Get-FileHash -LiteralPath $target).Hash -ne (Get-FileHash -LiteralPath $expected[$relative]).Hash) {
      "differs $relative"
    }
  }
  $extra = Get-ChildItem $installDir -Recurse -File -Force | ForEach-Object {
    $relative = $_.FullName.Substring($installDir.Length + 1)
    if ($relative -eq '.env' -or $expected.ContainsKey($relative)) { return }
    if (-not $messageFiles.ContainsKey($relative)) { "extra $relative"; return }
    if ((Get-FileHash -LiteralPath $_.FullName).Hash -ne (Get-FileHash -LiteralPath $messageFiles[$relative]).Hash) {
      "differs $relative"
    }
  }
  return @($diff) + @($extra)
}

function Assert-Install([string]$build, [string]$version, [string]$what) {
  $diff = Compare-Install $build $version
  if ($diff) { Fail "${what}: install folder does not match $(Split-Path -Leaf $build): $($diff -join ', ')" }
  $others = @(Get-ChildItem $installDir -File -Filter 'FilePickerAPI.messages.*.dll' |
    Where-Object Name -ne "FilePickerAPI.messages.$version.dll" | ForEach-Object Name)
  Pass "${what}: SHA-256 of all files match $(Split-Path -Leaf $build)$(if ($others) { ", kept $($others -join ', ')" })"
}

function Get-FileVersion([string]$path) {
  $info = (Get-Item -LiteralPath $path).VersionInfo
  # Из VS_FIXEDFILEINFO: строковую таблицу копия не меняет
  return "$($info.FileMajorPart).$($info.FileMinorPart).$($info.FileBuildPart)"
}

# Источник событий должен указывать на файл сообщений версии $version
function Assert-MessageFile([string]$version, [string]$what) {
  $expected = Join-Path $installDir "FilePickerAPI.messages.$version.dll"
  $messageFile = (Get-ItemProperty $sourceKey -ErrorAction SilentlyContinue).EventMessageFile
  if ($messageFile -ne $expected) { Fail "${what}: event source EventMessageFile is '$messageFile', expected '$expected'" }
  if (-not (Test-Path -LiteralPath $expected)) { Fail "${what}: $expected is missing" }
}

function Assert-Registration([string]$version, [string]$what) {
  $value = (Get-ItemProperty $runKey -ErrorAction SilentlyContinue).FilePickerAPI
  if ($value -ne "`"$exe`"") { Fail "${what}: Run value is '$value', expected '`"$exe`"'" }
  Assert-MessageFile $version $what
  Pass "${what}: Run value is registered, event source points to the $version message file"
}

function Assert-EnvFile([string]$content, [string]$what) {
  if (-not (Test-Path $envFile) -or (Get-Content $envFile -Raw).Trim() -ne $content) {
    Fail "${what}: .env is missing or changed"
  }
  Pass "${what}: .env is in place"
}

# Откат: удаление второго MSI и установка первого. Файлы сообщений обеих
# версий постоянные, а источник остаётся на файле второго MSI: в нём
# новейшая таблица, и первый MSI регистрацию не переписывает
function Invoke-Rollback([string]$what) {
  Uninstall-Msi $Msi2 "$what-remove-2"
  Install-Msi $Msi1 "$what-install-1"
  Assert-EnvFile $corsEnv "$what rollback"
  Assert-Install $Build1 $FileVersion1 "$what rollback"
  Assert-MessageFile $FileVersion2 "$what rollback"
  Pass "$what rollback: event source stays on the $FileVersion2 message file"
}

# --- экземпляры сервиса ----------------------------------------------

# Запуск как при входе пользователя: без консоли. Exe (attach) цепляется
# к консоли родителя, а у Start-Process родитель — pwsh шага с консолью
# раннера: exe работал бы «из консоли», и Restart Manager при
# обновлении закрывал бы его консольным сигналом, который получил бы и
# процесс раннера. Поэтому родитель exe — cmd без консоли
# (DETACHED_PROCESS); cmd /c ждёт exe и возвращает его код выхода.
# Id — PID exe (с -NoPid не ищется: короткий запуск успевает выйти)
function Start-Exe([string[]]$arguments = @(), [switch]$NoPid) {
  $cmd = Join-Path $env:SystemRoot 'System32\cmd.exe'
  $line = "`"$cmd`" /d /c `"`"$exe`"$(($arguments | ForEach-Object { " $_" }) -join '')`""
  $launcher = [SmokeWindows]::StartDetached($cmd, $line, $env:RUNNER_TEMP)
  $id = $null
  if (-not $NoPid) {
    $filter = "ParentProcessId = $($launcher.Id) AND Name = 'FilePickerAPI.exe'"
    $id = Wait-Until 15 { (Get-CimInstance Win32_Process -Filter $filter).ProcessId }
    if (-not $id) { Fail "FilePickerAPI.exe did not start (launcher PID $($launcher.Id))" }
  }
  return [pscustomobject]@{ Id = $id; Launcher = $launcher }
}

function Wait-Exit($process, [int]$seconds, [string]$what) {
  # Запуск через Start-Exe: код exe возвращает cmd
  if ($process.Launcher) { $process = $process.Launcher }
  if (-not $process.WaitForExit($seconds * 1000)) { Fail "$what did not exit in $seconds s" }
  return $process.ExitCode
}

function Test-Running($id) {
  return [bool]($id -and (Get-Process -Id $id -ErrorAction SilentlyContinue))
}

# Invoke-Http, Test-Health, Wait-HealthFor и Start-WithClosedInput —
# из smoke-common.ps1
function Wait-Health([int]$seconds, [string]$what) {
  if (-not (Wait-HealthFor $seconds)) { Fail "${what}: the service does not respond in $seconds s" }
}

function Get-PortOwner {
  $listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
    Select-Object -First 1
  if ($listener) { return [int]$listener.OwningProcess }
  return $null
}

# WM_CLOSE скрытому окну экземпляра. Окно ищется по классу и PID: окно
# того же класса есть и у второго экземпляра, пока он опрашивает порт
function Stop-Instance($id, [string]$what) {
  # Без проверки $null стал бы PID 0 (процесс Idle)
  if (-not $id) { Fail "${what}: no process to stop (nobody listens on port $port)" }
  $process = Get-Process -Id $id
  $null = $process.Handle
  $windows = @(Wait-Until 30 { , [SmokeWindows]::Find('FilePickerAPI.Instance', $id) })
  if (-not $windows) { Fail "${what}: hidden window of PID $id not found" }
  if ($windows.Count -ne 1) { Fail "${what}: PID $id has $($windows.Count) hidden windows" }
  if (-not [SmokeWindows]::Close($windows[0])) { Fail "${what}: PostMessage(WM_CLOSE) failed" }
  $code = Wait-Exit $process 30 "$what after WM_CLOSE"
  if ($code -ne 0) { Fail "${what}: PID $id exited with $code after WM_CLOSE, expected 0" }
  Pass "${what}: WM_CLOSE stopped PID $id with exit code 0"
}

function Stop-All {
  Get-Process FilePickerAPI -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
  Get-Process FilePickerAPI -ErrorAction SilentlyContinue | Wait-Process -Timeout 30 -ErrorAction SilentlyContinue
}

# --- журналы ---------------------------------------------------------

# Отметка — размер файла в байтах: читается только дописанный хвост,
# а не весь журнал за день
function Get-LogLength {
  if (Test-Path $serviceLog) { return [long](Get-Item $serviceLog).Length }
  return [long]0
}

# Текст файла журнала, дописанный после отметки Get-LogLength. Файл
# открыт сервисом на запись, поэтому чтение — с FileShare.ReadWrite
function Get-LogSince([long]$mark) {
  if (-not (Test-Path $serviceLog)) { return '' }
  $share = [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete
  $stream = [IO.FileStream]::new($serviceLog, 'Open', 'Read', $share)
  try {
    # Файл короче отметки — ротация в полночь: читается целиком
    if ($stream.Length -ge $mark) { [void]$stream.Seek($mark, 'Begin') }
    $reader = [IO.StreamReader]::new($stream, [Text.UTF8Encoding]::new($false))
    return $reader.ReadToEnd()
  } finally {
    $stream.Dispose()
  }
}

# Строку пишет поток сервиса, иногда уже после ответа по HTTP (2004 —
# поток создания директории): строка ждётся, а не проверяется один раз
function Assert-Log([long]$mark, [string]$text, [string]$what, [int]$seconds = 30) {
  if (-not (Wait-Until $seconds { (Get-LogSince $mark).Contains($text, [StringComparison]::Ordinal) })) {
    Write-Host "--- $serviceLog since the mark"
    Write-Host (Get-LogSince $mark)
    Fail "${what}: '$text' not found in $serviceLog in $seconds s"
  }
  Pass "${what}: '$text' in the log file"
}

function Get-EventMark {
  $last = Get-WinEvent -LogName Application -MaxEvents 1 -ErrorAction SilentlyContinue
  if ($last) { return [long]$last.RecordId }
  return [long]0
}

function Get-ServiceEvents([long]$mark, [int[]]$ids = @()) {
  $filter = "*[System[Provider[@Name='FilePickerAPI'] and (EventRecordID > $mark)]]"
  $events = @(Get-WinEvent -LogName Application -FilterXPath $filter -ErrorAction SilentlyContinue)
  if ($ids) { $events = @($events | Where-Object Id -in $ids) }
  return @($events | Sort-Object RecordId)
}

function Wait-ServiceEvent([long]$mark, [int]$id, [int]$seconds, [string]$text = '') {
  $found = @(Wait-Until $seconds {
    , @(Get-ServiceEvents $mark @($id) | Where-Object {
        -not $text -or ($_.Message -and $_.Message.Contains($text, [StringComparison]::Ordinal))
      })
  })
  if (-not $found) { Fail "event $id$(if ($text) { " with '$text'" }) not written in $seconds s" }
  return $found[-1]
}

function Show-Event($evt) {
  Write-Host "--- event $($evt.Id) ($($evt.LevelDisplayName), record $($evt.RecordId))"
  Write-Host $evt.Message
}

# Сообщение по таблице сообщений прочитано полностью (как в Event Viewer)
function Assert-Rendered($evt, [string]$what) {
  if (-not $evt.Message -or $evt.Message -match 'description for Event ID|Не найдено описание') {
    Show-Event $evt
    Fail "${what}: event $($evt.Id) message is not rendered"
  }
}

# Русские фрагменты сравниваются порядково (побайтно в UTF-16): так
# видно и подмену кодовой страницы, а не только отсутствие текста
function Assert-EventText($evt, [string[]]$fragments, [string]$what) {
  Assert-Rendered $evt $what
  foreach ($fragment in $fragments) {
    if (-not $evt.Message.Contains($fragment, [StringComparison]::Ordinal)) {
      Show-Event $evt
      Fail "${what}: event $($evt.Id) message has no '$fragment'"
    }
  }
}

$windowsError = 'код ошибки Windows [0-9]+ \([^)]+\)'
$succeeded = $false
$startMark = Get-EventMark

try {
  Stop-All
  if (Get-PortOwner) { Fail "port $port is busy before the smoke test" }
  if (Test-Path $installDir) { Fail "$installDir exists before the smoke test" }

  # 1. Установка: файлы, значение Run, источник событий, без bat-скриптов
  Step '1. install'
  Install-Msi $Msi1 'install-1'
  Assert-Install $Build1 $FileVersion1 'install'
  if (Get-ChildItem $installDir -Recurse -File -Force -Filter *.bat) { Fail 'install: bat scripts are installed' }
  Assert-Registration $FileVersion1 'install'
  if ((Get-FileVersion $exe) -ne $FileVersion1) { Fail "install: FILEVERSION is $(Get-FileVersion $exe), expected $FileVersion1" }

  # 2. Запуск без аргументов: версия сборки, папка филиала с раннера недоступна
  Step '2. start'
  $logMark = Get-LogLength
  $first = Start-Exe
  Wait-Health 60 'start'
  $schema = (Invoke-Http '/openapi.json').Content | ConvertFrom-Json
  if ($schema.info.version -ne $AppVersion) { Fail "start: /openapi.json version is $($schema.info.version), expected $AppVersion" }
  $files = Invoke-Http '/files'
  if ($files.Status -ne 404 -or ($files.Content | ConvertFrom-Json).detail -ne 'Files directory not found') {
    Fail "start: GET /files -> $($files.Status) $($files.Content)"
  }
  Pass "start: /openapi.json $AppVersion, GET /files -> 404 Files directory not found"

  # 3. Файл журнала в %LOCALAPPDATA% пользователя
  Step '3. log file'
  Assert-Log $logMark "INFO app.runner: Сервис запущен: версия $AppVersion" 'log file'
  Assert-Log $logMark "пользователь $user" 'log file'
  # Start-Exe запускает exe без консоли (DETACHED_PROCESS), как при входе
  Assert-Log $logMark 'запуск без окна' 'log file'
  Assert-Log $logMark "WARNING app.runner: Не удалось создать директорию с файлами $FilesDirectory" 'log file'

  # 4. Второй запуск и аргументы
  Step '4. second instance'
  $mark = Get-EventMark
  $logMark = Get-LogLength
  $code = Wait-Exit (Start-Exe -NoPid) 15 'second instance'
  if ($code -ne 0) { Fail "second instance exited with $code, expected 0" }
  Assert-Log $logMark 'Сервис уже работает в другом процессе' 'second instance'
  $evt = Wait-ServiceEvent $mark 1005 15
  Assert-EventText $evt @("PID $($first.Id), версия $AppVersion") 'second instance'
  $events = @(Get-ServiceEvents $mark @(1005, 1002))
  if ($events.Count -ne 1) { Fail "second instance: events $(($events.Id) -join ', '), expected one 1005" }
  Pass 'second instance: exit code 0 and one 1005 with the first PID'
  $code = Wait-Exit (Start-Exe @('D:\x') -NoPid) 30 'FilePickerAPI.exe D:\x'
  if ($code -ne 2) { Fail "FilePickerAPI.exe D:\x exited with $code, expected 2" }
  Pass 'FilePickerAPI.exe D:\x -> 2'

  # 5. Остановка через WM_CLOSE
  Step '5. WM_CLOSE'
  $mark = Get-EventMark
  Stop-Instance $first.Id 'WM_CLOSE'
  Assert-EventText (Wait-ServiceEvent $mark 1001 15) @('Сервис остановлен: сообщение WM_CLOSE') 'WM_CLOSE'

  # 6. Обновление остановленного сервиса, отказ той же и старой версии, откат
  Step '6. upgrade a stopped service'
  Set-Content $envFile $corsEnv -Encoding utf8NoBOM
  # Шаги 2–5 читали тексты событий: служба журнала событий загрузила файл
  # сообщений первого MSI и держит его. Обновление его не заменяет, а
  # добавляет файл второй версии, поэтому Restart Manager никого не
  # закрывает (сервис остановлен), и PID EventLog прежний (Invoke-Msi)
  $eventLogPid = (Get-EventLogService).ProcessId
  Install-Msi $Msi2 'upgrade-2'
  $shutdowns = Get-RestartManagerShutdowns $script:lastMsiLog
  if ($shutdowns) {
    $shutdowns | ForEach-Object { Write-Host "  $_" }
    Fail 'upgrade: Restart Manager shut down applications holding files in use'
  }
  Pass "upgrade: EventLog keeps PID $eventLogPid, Restart Manager shut nothing down"
  Assert-EnvFile $corsEnv 'upgrade'
  Assert-Registration $FileVersion2 'upgrade'
  Assert-Install $Build2 $FileVersion2 'upgrade'
  if ((Get-FileVersion $exe) -ne $FileVersion2) { Fail "upgrade: FILEVERSION is $(Get-FileVersion $exe), expected $FileVersion2" }
  foreach ($case in @(@($Msi1, 'older'), @($Msi2Rebuilt, 'same-version'))) {
    $code = Invoke-Msi '/i' $case[0] "refuse-$($case[1])"
    # Успех — и 1641, и 3010 (установлено, нужна перезагрузка)
    if ($code -in 0, 1641, 3010) { Fail "$($case[1]) MSI over the second one was installed ($code)" }
    Assert-Install $Build2 $FileVersion2 "$($case[1]) MSI refused ($code)"
  }
  Invoke-Rollback 'stopped service'
  $evt = @(Get-ServiceEvents $startMark @(1001))[-1]
  Assert-EventText $evt @('Сервис остановлен') 'after rollback'
  Pass 'after rollback: earlier events are rendered'

  # 7. Порт занят другой программой, затем освобождается
  Step '7. port busy'
  $holderCommand = '$l = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Any, ' + $port + '); $l.Start(); Start-Sleep 3600'
  $script:portHolder = Start-Process pwsh -ArgumentList '-NoProfile', '-Command', $holderCommand -PassThru -WindowStyle Hidden
  if (-not (Wait-Until 30 { (Get-PortOwner) -eq $script:portHolder.Id })) { Fail 'port busy: the holder does not listen' }
  $mark = Get-EventMark
  $busy = Start-Exe
  Assert-EventText (Wait-ServiceEvent $mark 1002 60 'попытка 1 из') @('HTTP-сервер не работает (попытка 1 из') 'port busy'
  Stop-Process -Id $script:portHolder.Id -Force
  $script:portHolder = $null
  # Вторая попытка — через 10 с после первой, третья — ещё через 30
  Wait-Health 30 'port released'
  if (-not (Test-Running $busy.Id) -or (Get-PortOwner) -ne $busy.Id) { Fail 'port released: another process answers' }
  Pass 'port released: the service responds before the third attempt'
  Stop-Instance $busy.Id 'port released'

  # 8. FILES_DIRECTORY в .env — код 1 без повторов
  Step '8. FILES_DIRECTORY in .env'
  Set-Content $envFile 'FILES_DIRECTORY=D:\x' -Encoding utf8NoBOM
  $mark = Get-EventMark
  $code = Wait-Exit (Start-Exe -NoPid) 60 'FILES_DIRECTORY in .env'
  if ($code -ne 1) { Fail "FILES_DIRECTORY in .env: exit code $code, expected 1" }
  $events = @(Get-ServiceEvents $mark @(1000, 1001, 1002))
  if ($events.Count -ne 1 -or $events[0].Id -ne 1002) {
    Fail "FILES_DIRECTORY in .env: events $(($events.Id) -join ', '), expected one 1002"
  }
  Set-Content $envFile $corsEnv -Encoding utf8NoBOM
  Pass 'FILES_DIRECTORY in .env: exit code 1 and one 1002 without retries'

  # 9. Право записи Users на папку установки — 1003
  Step '9. build folder permissions'
  & icacls.exe $installDir /grant "${usersSid}:(OI)(CI)M" | Out-Null
  if ($LASTEXITCODE) { Fail "icacls /grant exited with $LASTEXITCODE" }
  $permissionsMark = Get-EventMark
  try {
    $mark = $permissionsMark
    $unsafe = Start-Exe
    $evt = Wait-ServiceEvent $mark 1003 120
    Assert-EventText $evt @('Права на папку сборки нарушены', $installDir) 'permissions'
    Show-Event $evt
    Wait-Health 60 'permissions'
    Stop-Instance $unsafe.Id 'permissions'
  } finally {
    & icacls.exe $installDir /remove:g $usersSid | Out-Null
  }
  Pass 'permissions: 1003 with the list of violations'

  # 10. Тексты событий: Get-WinEvent и wevtutil
  Step '10. event texts'
  $all = @(Get-ServiceEvents $startMark)
  $all | ForEach-Object { Show-Event $_ }
  # До выдачи права Users 1003 не было: права папки установки в порядке
  $early = @($all | Where-Object { $_.Id -eq 1003 -and $_.RecordId -le $permissionsMark })
  if ($early) { Fail 'event texts: 1003 before the permissions were changed' }
  $checks = @(
    @{ Id = 1000; Level = 4; Text = @("Сервис запущен: версия $AppVersion", $FilesDirectory, "пользователь $user", 'запуск без окна') },
    @{ Id = 2004; Level = 3; Text = @("Не удалось создать директорию с файлами $FilesDirectory"); Pattern = $windowsError },
    @{ Id = 2000; Level = 3; Text = @("Папка с файлами $FilesDirectory недоступна", 'Ответ клиенту: 404'); Pattern = $windowsError },
    @{ Id = 1001; Level = 4; Text = @('Сервис остановлен: сообщение WM_CLOSE') },
    # FILES_DIRECTORY из .env pydantic называет по полю: files_directory
    @{ Id = 1002; Level = 2; Text = @('Ошибка настроек, сервис не запущен', 'files_directory', 'extra_forbidden', 'Traceback (most recent call last)') }
  )
  foreach ($check in $checks) {
    $candidates = @($all | Where-Object Id -eq $check.Id)
    if (-not $candidates) { Fail "event texts: no event $($check.Id)" }
    $candidates | ForEach-Object { Assert-Rendered $_ 'event texts' }
    $matching = @($candidates | Where-Object {
        $message = $_.Message
        $_.Level -eq $check.Level -and
          -not ($check.Text | Where-Object { -not $message.Contains($_, [StringComparison]::Ordinal) }) -and
          (-not $check.Pattern -or $message -match $check.Pattern)
      })
    if (-not $matching) {
      Fail "event texts: no event $($check.Id) with level $($check.Level), '$($check.Text -join "', '")'$(if ($check.Pattern) { " and /$($check.Pattern)/" })"
    }
    Pass "event texts: $($check.Id) ($($matching[0].LevelDisplayName))"
  }
  # Консоль искажает кириллицу в выводе wevtutil: только наличие описания
  $query = "*[System[Provider[@Name='FilePickerAPI'] and (EventRecordID > $startMark)]]"
  $text = (& wevtutil.exe qe Application "/q:$query" /f:text | Out-String)
  if ($LASTEXITCODE) { Fail "wevtutil exited with $LASTEXITCODE" }
  if (-not $text.Trim()) { Fail 'wevtutil: no events' }
  if ($text -match 'description for Event ID|Не найдено описание') { Write-Host $text; Fail 'wevtutil: event description not found' }
  Pass "wevtutil: $($all.Count) events with descriptions"

  # 11. Второй MSI поверх работающего экземпляра того же пользователя
  Step '11. upgrade a running service (same user)'
  $old = Start-Exe
  Wait-Health 60 'running before upgrade'
  $mark = Get-EventMark
  $code = Invoke-Msi '/i' $Msi2 'upgrade-running'
  Assert-MsiCode $code @(0, 3010) 'upgrade running'
  Assert-EnvFile $corsEnv 'upgrade running'
  Assert-Registration $FileVersion2 'upgrade running'
  if ($code -eq 0) {
    Assert-Install $Build2 $FileVersion2 'upgrade running'
    Assert-EventText (Wait-ServiceEvent $mark 1001 30) @('Сервис остановлен: обновление или удаление программы') 'upgrade running'
    $owner = Wait-Until 60 { $id = Get-PortOwner; if ($id -and $id -ne $old.Id) { $id } }
    if (-not $owner) { Fail 'upgrade running: no new process listens on the port' }
    $image = (Get-Process -Id $owner).Path
    if ($image -ne $exe) { Fail "upgrade running: port $port is held by $image" }
    if ((Get-FileVersion $image) -ne $FileVersion2) { Fail "upgrade running: new process FILEVERSION is $(Get-FileVersion $image)" }
    Wait-Health 30 'restarted after upgrade'
    if ((Invoke-Http '/files').Status -ne 404) { Fail 'restarted after upgrade: GET /files is not 404' }
    Pass "upgrade running: restarted as PID $owner with FILEVERSION $FileVersion2"
  } else {
    Write-Host 'upgrade running: 3010, replacement is postponed until reboot'
    $owner = Get-PortOwner
  }

  # 12. Откат, затем второй MSI от SYSTEM (как SCCM) поверх работающего
  Step '12. upgrade a running service (SYSTEM)'
  if ($owner) { Stop-Instance $owner 'before SYSTEM upgrade' }
  Invoke-Rollback 'SYSTEM upgrade'
  $old = Start-Exe
  Wait-Health 60 'running before SYSTEM upgrade'
  $paths = '/openapi.json', '/files', '/files/missing-file.txt'
  $before = @{}
  foreach ($path in $paths) { $before[$path] = Invoke-Http $path }
  $mark = Get-EventMark
  $logMark = Get-LogLength
  $log = Join-Path $LogDir 'system-upgrade.log'
  $script:lastMsiLog = $log
  Show-FilesInUse 'before msiexec /i (SYSTEM)'
  $action = New-ScheduledTaskAction -Execute msiexec.exe -Argument "/i `"$Msi2`" /qn /norestart /l*v! `"$log`""
  Register-ScheduledTask $taskName -Action $action -User SYSTEM -RunLevel Highest -Force | Out-Null
  Start-ScheduledTask $taskName
  # 267009 — задача выполняется, 267011 — ещё не запускалась
  $done = Wait-Until 600 {
    $info = Get-ScheduledTaskInfo $taskName
    (Get-ScheduledTask $taskName).State -ne 'Running' -and $info.LastTaskResult -notin 267009, 267011
  }
  if (-not $done) { Fail 'SYSTEM upgrade: msiexec did not finish in 10 min' }
  $code = (Get-ScheduledTaskInfo $taskName).LastTaskResult
  Unregister-ScheduledTask $taskName -Confirm:$false
  Write-Host "msiexec /i (SYSTEM) -> $code"
  Assert-MsiCode $code @(0, 3010) 'SYSTEM upgrade'
  if ($code -eq 0) {
    Assert-Install $Build2 $FileVersion2 'SYSTEM upgrade'
  } else {
    # Сверяются все файлы. Файлы сообщений обеих версий Compare-Install
    # сверяет сам, остальные должны совпадать с первым или вторым MSI
    $diff = @(Compare-Install $Build1 $FileVersion1) | Where-Object {
      $_ -notmatch '^differs (.+)$' -or
        (Get-FileHash (Join-Path $installDir $Matches[1])).Hash -ne (Get-FileHash (Join-Path $Build2 $Matches[1])).Hash
    }
    if ($diff) { Fail "SYSTEM upgrade (3010): files match neither MSI: $($diff -join ', ')" }
    $pending = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager' -ErrorAction SilentlyContinue).PendingFileRenameOperations
    if (-not ($pending | Where-Object { $_ -like "*$installDir*" })) {
      Fail 'SYSTEM upgrade (3010): no PendingFileRenameOperations for the install folder'
    }
    Pass 'SYSTEM upgrade (3010): each file matches the first or the second MSI, replacement is pending'
  }
  if (-not (Test-Running $old.Id) -or (Get-PortOwner) -ne $old.Id) { Fail 'SYSTEM upgrade: the old process does not hold the port' }
  if (@(Get-ServiceEvents $mark @(1001)).Count) { Fail 'SYSTEM upgrade: the old process wrote 1001' }
  foreach ($path in $paths) {
    $after = Invoke-Http $path
    if (-not $after -or $after.Status -ne $before[$path].Status -or $after.Content -ne $before[$path].Content) {
      Fail "SYSTEM upgrade: GET $path -> $($after.Status), before $($before[$path].Status)"
    }
  }
  $logText = Get-LogSince $logMark
  # Не Traceback: повторные отказы недоступной папки пишутся в файл с
  # трассировкой, это не ошибка загрузки
  if ($logText -match 'ImportError|ModuleNotFoundError|DLL load failed') {
    Write-Host $logText
    Fail 'SYSTEM upgrade: module or DLL load errors in the log file'
  }
  Pass "SYSTEM upgrade: PID $($old.Id) keeps working without 1001"
  $mark = Get-EventMark
  Stop-Instance $old.Id 'after SYSTEM upgrade'
  Assert-EventText (Wait-ServiceEvent $mark 1001 15) @('Сервис остановлен: сообщение WM_CLOSE') 'after SYSTEM upgrade'

  # 13. Удаление: значения Run нет, источник и файлы сообщений остались
  Step '13. uninstall'
  $code = Invoke-Msi '/x' $Msi2 'uninstall'
  Assert-MsiCode $code @(0, 3010) 'uninstall'
  if ($null -ne (Get-ItemProperty $runKey -ErrorAction SilentlyContinue).FilePickerAPI) { Fail 'uninstall: Run value remains' }
  Assert-MessageFile $FileVersion2 'uninstall'
  foreach ($name in $messageFiles.Keys) {
    if (-not (Test-Path -LiteralPath (Join-Path $installDir $name))) { Fail "uninstall: $name is removed" }
  }
  $evt = @(Get-ServiceEvents $startMark @(1000))[0]
  Assert-EventText $evt @("Сервис запущен: версия $AppVersion", $FilesDirectory) 'uninstall'
  Pass 'uninstall: no Run value; event source and message files remain, 1000 is rendered'

  # 14. run_test.bat из папки сборки архива
  Step '14. run_test.bat'
  $runDir = Join-Path $env:RUNNER_TEMP 'msi-smoke-run-test'
  Remove-Item -Recurse -Force $runDir -ErrorAction SilentlyContinue
  New-Item -ItemType Directory $runDir | Out-Null
  Copy-Item -Recurse $BuildDir (Join-Path $runDir 'FilePickerAPI')
  $cmd = Join-Path $env:SystemRoot 'System32\cmd.exe'
  $bat = Start-WithClosedInput $cmd @('/c', (Join-Path $runDir 'FilePickerAPI\run_test.bat')) $runDir
  Wait-Health 60 'run_test.bat'
  Stop-Instance (Get-PortOwner) 'run_test.bat'
  $null = Wait-Exit $bat 30 'run_test.bat after FilePickerAPI.exe stopped'
  Pass 'run_test.bat: the service responds'

  $succeeded = $true
} catch {
  Write-Host "::error::MSI smoke test: $_"
  Write-Host $_.ScriptStackTrace
} finally {
  Stop-All
  if ($script:portHolder) { Stop-Process -Id $script:portHolder.Id -Force -ErrorAction SilentlyContinue }
  if (-not $succeeded) {
    # Удаление задачи не останавливает её запущенный экземпляр: msiexec
    # от SYSTEM (шаг 12 по сроку) продолжал бы установку, и /x ниже
    # получил бы 1618. Задача останавливается, затем — ожидание конца
    # операции Windows Installer (мьютекс _MSIExecute), не дольше 2 мин
    Stop-ScheduledTask $taskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask $taskName -Confirm:$false -ErrorAction SilentlyContinue
    $idle = Wait-Until 120 {
      $mutex = $null
      if ([Threading.Mutex]::TryOpenExisting('Global\_MSIExecute', [ref]$mutex)) { $mutex.Dispose(); $false } else { $true }
    }
    if (-not $idle) { Write-Host 'cleanup: Windows Installer is still busy, uninstall may return 1618' }
    if (Test-Path $installDir) { & icacls.exe $installDir /remove:g $usersSid 2>&1 | Out-Null }
    foreach ($package in $Msi1, $Msi2, $Msi2Rebuilt) {
      # Не установленный пакет: 1605, это не ошибка очистки
      # Короткое ожидание: зависший msiexec не держит джоб, он останавливается
      try { $null = Invoke-Msi '/x' $package 'cleanup' 120 } catch { Write-Host "cleanup: $_" }
    }
  }
}
if (-not $succeeded) { exit 1 }
Write-Host 'MSI smoke test passed'
exit 0
