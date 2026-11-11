# Дымовая проверка MSI (user-logon-autostart, задача 6.3): установка,
# работа, обновление, откат и удаление на раннере Windows по шагам 1–14
# раздела «Тесты без Windows и проверка на Windows в CI» design.md.
#
# Раннер работает от администратора без интерактивного входа, поэтому
# вход пользователя заменяет запуск exe без аргументов (Start-Process).
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
$Msi1, $Msi2, $Msi2Rebuilt, $Build1, $Build2, $BuildDir =
  $Msi1, $Msi2, $Msi2Rebuilt, $Build1, $Build2, $BuildDir | ForEach-Object { (Resolve-Path $_).Path }
New-Item -ItemType Directory -Force $LogDir | Out-Null
$LogDir = (Resolve-Path $LogDir).Path

$installDir = Join-Path $env:ProgramFiles 'FilePickerAPI'
$exe = Join-Path $installDir 'FilePickerAPI.exe'
$envFile = Join-Path $installDir '.env'
$messageDll = 'FilePickerAPI.messages.dll'
$runKey = 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Run'
$sourceKey = 'HKLM:\SYSTEM\CurrentControlSet\Services\EventLog\Application\FilePickerAPI'
$serviceLog = Join-Path $env:LOCALAPPDATA 'FilePickerAPI\logs\FilePickerAPI.log'
$baseUrl = 'http://127.0.0.1:8000'
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

# Без Start-Process -Wait: он ждёт и дочерние процессы, а Restart
# Manager запускает exe заново, и ожидание длилось бы, пока exe работает
function Invoke-Msi([string]$action, [string]$package, [string]$label) {
  $script:msiCount++
  $log = Join-Path $LogDir ('{0:d2}-{1}.log' -f $script:msiCount, $label)
  $arguments = @($action, "`"$package`"", '/qn', '/norestart', '/l*v', "`"$log`"")
  $process = Start-Process msiexec.exe -ArgumentList $arguments -PassThru
  $null = $process.Handle  # иначе ExitCode после выхода недоступен
  if (-not $process.WaitForExit(600000)) { Fail "msiexec $action $label did not exit in 10 min" }
  Write-Host "msiexec $action $(Split-Path -Leaf $package) ($label) -> $($process.ExitCode)"
  return $process.ExitCode
}

function Install-Msi([string]$package, [string]$label) {
  $code = Invoke-Msi '/i' $package $label
  if ($code -ne 0) { Fail "msiexec /i $label exited with $code, expected 0" }
}

function Uninstall-Msi([string]$package, [string]$label) {
  $code = Invoke-Msi '/x' $package $label
  if ($code -ne 0) { Fail "msiexec /x $label exited with $code, expected 0" }
}

# --- файлы установки -------------------------------------------------

# Расхождения папки установки с папкой сборки, из которой собран MSI:
# SHA-256 каждого файла, кроме bat-скриптов (их нет в пакете), и лишние
# файлы, кроме .env
function Compare-Install([string]$build, [string[]]$exclude = @()) {
  $expected = @{}
  Get-ChildItem $build -Recurse -File | Where-Object Extension -ne '.bat' | ForEach-Object {
    $expected[$_.FullName.Substring($build.Length + 1)] = $_.FullName
  }
  $diff = foreach ($relative in $expected.Keys) {
    if ($relative -in $exclude) { continue }
    $target = Join-Path $installDir $relative
    if (-not (Test-Path -LiteralPath $target)) { "missing $relative"; continue }
    if ((Get-FileHash -LiteralPath $target).Hash -ne (Get-FileHash -LiteralPath $expected[$relative]).Hash) {
      "differs $relative"
    }
  }
  $extra = Get-ChildItem $installDir -Recurse -File | ForEach-Object {
    $relative = $_.FullName.Substring($installDir.Length + 1)
    if ($relative -ne '.env' -and -not $expected.ContainsKey($relative)) { "extra $relative" }
  }
  return @($diff) + @($extra)
}

function Assert-Install([string]$build, [string]$what, [string[]]$exclude = @()) {
  $diff = Compare-Install $build $exclude
  if ($diff) { Fail "${what}: install folder does not match $(Split-Path -Leaf $build): $($diff -join ', ')" }
  Pass "${what}: SHA-256 of all files match $(Split-Path -Leaf $build)$(if ($exclude) { " except $($exclude -join ', ')" })"
}

function Get-FileVersion([string]$path) {
  $info = (Get-Item -LiteralPath $path).VersionInfo
  # Из VS_FIXEDFILEINFO: строковую таблицу копия не меняет
  return "$($info.FileMajorPart).$($info.FileMinorPart).$($info.FileBuildPart)"
}

function Assert-Registration([string]$what) {
  $value = (Get-ItemProperty $runKey -ErrorAction SilentlyContinue).FilePickerAPI
  if ($value -ne "`"$exe`"") { Fail "${what}: Run value is '$value', expected '`"$exe`"'" }
  $messageFile = (Get-ItemProperty $sourceKey -ErrorAction SilentlyContinue).EventMessageFile
  if ($messageFile -ne (Join-Path $installDir $messageDll)) {
    Fail "${what}: event source EventMessageFile is '$messageFile'"
  }
  Pass "${what}: Run value and event source are registered"
}

function Assert-EnvFile([string]$content, [string]$what) {
  if (-not (Test-Path $envFile) -or (Get-Content $envFile -Raw).Trim() -ne $content) {
    Fail "${what}: .env is missing or changed"
  }
  Pass "${what}: .env is in place"
}

# Откат: удаление второго MSI и установка первого. Файл сообщений
# постоянный и остаётся от второго MSI: в нём новейшая таблица
function Invoke-Rollback([string]$what) {
  Uninstall-Msi $Msi2 "$what-remove-2"
  Install-Msi $Msi1 "$what-install-1"
  Assert-EnvFile $corsEnv "$what rollback"
  Assert-Install $Build1 "$what rollback" @($messageDll)
  $dll = Join-Path $installDir $messageDll
  if ((Get-FileHash $dll).Hash -ne (Get-FileHash (Join-Path $Build2 $messageDll)).Hash) {
    Fail "$what rollback: $messageDll is not the newest one"
  }
  Pass "$what rollback: $messageDll stays from the second MSI ($(Get-FileVersion $dll))"
}

# --- экземпляры сервиса ----------------------------------------------

function Start-Exe([string[]]$arguments = @()) {
  $parameters = @{ FilePath = $exe; PassThru = $true; WorkingDirectory = $env:RUNNER_TEMP }
  if ($arguments) { $parameters.ArgumentList = $arguments }
  $process = Start-Process @parameters
  $null = $process.Handle
  return $process
}

function Wait-Exit($process, [int]$seconds, [string]$what) {
  if (-not $process.WaitForExit($seconds * 1000)) { Fail "$what did not exit in $seconds s" }
  return $process.ExitCode
}

function Invoke-Http([string]$path) {
  try {
    $response = Invoke-WebRequest -Uri "$baseUrl$path" -NoProxy -TimeoutSec 5 -SkipHttpErrorCheck
    return [pscustomobject]@{ Status = [int]$response.StatusCode; Content = [string]$response.Content }
  } catch {
    return $null
  }
}

function Test-Health {
  $response = Invoke-Http '/openapi.json'
  return $response -and $response.Status -eq 200 -and ($response.Content | ConvertFrom-Json).info.title -eq 'File Picker API'
}

function Wait-Health([int]$seconds, [string]$what) {
  if (-not (Wait-Until $seconds { Test-Health })) { Fail "${what}: the service does not respond in $seconds s" }
}

function Get-PortOwner {
  $listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
    Select-Object -First 1
  if ($listener) { return [int]$listener.OwningProcess }
  return $null
}

# WM_CLOSE скрытому окну экземпляра. Окно ищется по классу и PID: окно
# того же класса есть и у второго экземпляра, пока он опрашивает порт
function Stop-Instance([int]$id, [string]$what) {
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

function Get-LogLength {
  if (Test-Path $serviceLog) { return (Get-Content $serviceLog -Raw -Encoding utf8).Length }
  return 0
}

# Текст файла журнала, дописанный после отметки Get-LogLength
function Get-LogSince([int]$mark) {
  if (-not (Test-Path $serviceLog)) { return '' }
  $content = Get-Content $serviceLog -Raw -Encoding utf8
  if ($content.Length -lt $mark) { return $content }  # ротация в полночь
  return $content.Substring($mark)
}

function Assert-Log([int]$mark, [string]$text, [string]$what) {
  if (-not (Get-LogSince $mark).Contains($text, [StringComparison]::Ordinal)) {
    Fail "${what}: '$text' not found in $serviceLog"
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
  Assert-Install $Build1 'install'
  if (Get-ChildItem $installDir -Recurse -File -Filter *.bat) { Fail 'install: bat scripts are installed' }
  Assert-Registration 'install'
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
  Assert-Log $logMark "WARNING app.runner: Не удалось создать директорию с файлами $FilesDirectory" 'log file'

  # 4. Второй запуск и аргументы
  Step '4. second instance'
  $mark = Get-EventMark
  $logMark = Get-LogLength
  $code = Wait-Exit (Start-Exe) 15 'second instance'
  if ($code -ne 0) { Fail "second instance exited with $code, expected 0" }
  Assert-Log $logMark 'Сервис уже работает в другом процессе' 'second instance'
  $evt = Wait-ServiceEvent $mark 1005 15
  Assert-EventText $evt @("PID $($first.Id), версия $AppVersion") 'second instance'
  $events = @(Get-ServiceEvents $mark @(1005, 1002))
  if ($events.Count -ne 1) { Fail "second instance: events $(($events.Id) -join ', '), expected one 1005" }
  Pass 'second instance: exit code 0 and one 1005 with the first PID'
  $code = Wait-Exit (Start-Exe @('D:\x')) 30 'FilePickerAPI.exe D:\x'
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
  Install-Msi $Msi2 'upgrade-2'
  Assert-EnvFile $corsEnv 'upgrade'
  Assert-Registration 'upgrade'
  Assert-Install $Build2 'upgrade'
  if ((Get-FileVersion $exe) -ne $FileVersion2) { Fail "upgrade: FILEVERSION is $(Get-FileVersion $exe), expected $FileVersion2" }
  foreach ($case in @(@($Msi1, 'older'), @($Msi2Rebuilt, 'same-version'))) {
    $code = Invoke-Msi '/i' $case[0] "refuse-$($case[1])"
    if ($code -eq 0) { Fail "$($case[1]) MSI over the second one was installed" }
    Assert-Install $Build2 "$($case[1]) MSI refused ($code)"
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
  if ($busy.HasExited -or (Get-PortOwner) -ne $busy.Id) { Fail 'port released: another process answers' }
  Pass 'port released: the service responds before the third attempt'
  Stop-Instance $busy.Id 'port released'

  # 8. FILES_DIRECTORY в .env — код 1 без повторов
  Step '8. FILES_DIRECTORY in .env'
  Set-Content $envFile 'FILES_DIRECTORY=D:\x' -Encoding utf8NoBOM
  $mark = Get-EventMark
  $code = Wait-Exit (Start-Exe) 60 'FILES_DIRECTORY in .env'
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
    @{ Id = 1000; Level = 4; Text = @("Сервис запущен: версия $AppVersion", $FilesDirectory, "пользователь $user") },
    @{ Id = 2004; Level = 3; Text = @("Не удалось создать директорию с файлами $FilesDirectory"); Pattern = $windowsError },
    @{ Id = 2000; Level = 3; Text = @("Папка с файлами $FilesDirectory недоступна", 'Ответ клиенту: 404'); Pattern = $windowsError },
    @{ Id = 1001; Level = 4; Text = @('Сервис остановлен: сообщение WM_CLOSE') },
    @{ Id = 1002; Level = 2; Text = @('Ошибка настроек, сервис не запущен', 'FILES_DIRECTORY', 'Traceback (most recent call last)') }
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
  if ($code -notin 0, 3010) { Fail "upgrade running: msiexec exited with $code, expected 0 or 3010" }
  Assert-EnvFile $corsEnv 'upgrade running'
  Assert-Registration 'upgrade running'
  if ($code -eq 0) {
    Assert-Install $Build2 'upgrade running'
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
  $action = New-ScheduledTaskAction -Execute msiexec.exe -Argument "/i `"$Msi2`" /qn /norestart /l*v `"$log`""
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
  if ($code -notin 0, 3010) { Fail "SYSTEM upgrade: msiexec exited with $code, expected 0 or 3010" }
  if ($code -eq 0) {
    Assert-Install $Build2 'SYSTEM upgrade'
  } else {
    $diff = @(Compare-Install $Build1 @($messageDll)) | Where-Object {
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
  if ($old.HasExited -or (Get-PortOwner) -ne $old.Id) { Fail 'SYSTEM upgrade: the old process does not hold the port' }
  if (@(Get-ServiceEvents $mark @(1001)).Count) { Fail 'SYSTEM upgrade: the old process wrote 1001' }
  foreach ($path in $paths) {
    $after = Invoke-Http $path
    if (-not $after -or $after.Status -ne $before[$path].Status -or $after.Content -ne $before[$path].Content) {
      Fail "SYSTEM upgrade: GET $path -> $($after.Status), before $($before[$path].Status)"
    }
  }
  $logText = Get-LogSince $logMark
  if ($logText -match 'ImportError|ModuleNotFoundError|DLL load failed|Traceback') {
    Write-Host $logText
    Fail 'SYSTEM upgrade: module or DLL load errors in the log file'
  }
  Pass "SYSTEM upgrade: PID $($old.Id) keeps working without 1001"
  $mark = Get-EventMark
  Stop-Instance $old.Id 'after SYSTEM upgrade'
  Assert-EventText (Wait-ServiceEvent $mark 1001 15) @('Сервис остановлен: сообщение WM_CLOSE') 'after SYSTEM upgrade'

  # 13. Удаление: значения Run нет, источник и файл сообщений остались
  Step '13. uninstall'
  $code = Invoke-Msi '/x' $Msi2 'uninstall'
  if ($code -notin 0, 3010) { Fail "uninstall: msiexec exited with $code, expected 0 or 3010" }
  if ($null -ne (Get-ItemProperty $runKey -ErrorAction SilentlyContinue).FilePickerAPI) { Fail 'uninstall: Run value remains' }
  if ((Get-ItemProperty $sourceKey -ErrorAction SilentlyContinue).EventMessageFile -ne (Join-Path $installDir $messageDll)) {
    Fail 'uninstall: event source registration is removed'
  }
  if (-not (Test-Path (Join-Path $installDir $messageDll))) { Fail "uninstall: $messageDll is removed" }
  $evt = @(Get-ServiceEvents $startMark @(1000))[0]
  Assert-EventText $evt @("Сервис запущен: версия $AppVersion", $FilesDirectory) 'uninstall'
  Pass 'uninstall: no Run value; event source and message file remain, 1000 is rendered'

  # 14. run_test.bat из папки сборки архива
  Step '14. run_test.bat'
  $runDir = Join-Path $env:RUNNER_TEMP 'msi-smoke-run-test'
  Remove-Item -Recurse -Force $runDir -ErrorAction SilentlyContinue
  New-Item -ItemType Directory $runDir | Out-Null
  Copy-Item -Recurse $BuildDir (Join-Path $runDir 'FilePickerAPI')
  $psi = [Diagnostics.ProcessStartInfo]::new((Join-Path $env:SystemRoot 'System32\cmd.exe'))
  $psi.ArgumentList.Add('/c')
  $psi.ArgumentList.Add((Join-Path $runDir 'FilePickerAPI\run_test.bat'))
  $psi.WorkingDirectory = $runDir
  $psi.UseShellExecute = $false
  # Ввод закрыт, чтобы pause в run_test.bat не ждал нажатия клавиши
  $psi.RedirectStandardInput = $true
  $bat = [Diagnostics.Process]::Start($psi)
  $bat.StandardInput.Close()
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
    Unregister-ScheduledTask $taskName -Confirm:$false -ErrorAction SilentlyContinue
    if (Test-Path $installDir) { & icacls.exe $installDir /remove:g $usersSid 2>&1 | Out-Null }
    foreach ($package in $Msi1, $Msi2, $Msi2Rebuilt) {
      # Не установленный пакет: 1605, это не ошибка очистки
      try { $null = Invoke-Msi '/x' $package 'cleanup' } catch { Write-Host "cleanup: $_" }
    }
  }
}
if (-not $succeeded) { exit 1 }
Write-Host 'MSI smoke test passed'
exit 0
