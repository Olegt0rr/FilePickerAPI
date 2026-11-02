# Дымовая проверка собранного exe с прежними bat-скриптами
# (user-logon-autostart, задачи 2.3, 2.4 и 3.1):
#   0. в сборке есть FilePickerAPI.messages.dll;
#   1. exe с аргументами завершается с кодом 2;
#   2. .env с FILES_DIRECTORY в папке exe не даёт сервису запуститься
#      (код 1), хотя exe запущен из другой папки: он переходит в свою;
#      ошибка записана в файл журнала пользователя;
#   3. run_production.bat запускает exe, дожидается ответа и пишет
#      строку об успешном запуске в startup.log; запуск Uvicorn виден
#      в файле журнала, хотя у exe нет консоли;
#   4. run_test.bat запускает exe и ждёт его до остановки.
#
# Параметр — папка сборки (dist/FilePickerAPI). Проверяется её копия во
# временной папке раннера: созданный проверкой .env в архив не попадает.
#
# Используется в build-exe.yml после сборки первого филиала матрицы.
# Скрипт PowerShell 7 (pwsh, shell: pwsh в workflow), как и sign.ps1:
# Invoke-RestMethod -NoProxy, кодировка utf8NoBOM и
# ProcessStartInfo.ArgumentList в Windows PowerShell 5.1 нет.

param([Parameter(Mandatory)][string]$BuildDir)

$ErrorActionPreference = 'Stop'
$healthUrl = 'http://127.0.0.1:8000/openapi.json'
$cmd = Join-Path $env:SystemRoot 'System32\cmd.exe'

# Рабочая папка запусков — родительская, а не папка exe
$workDir = Join-Path $env:RUNNER_TEMP 'startup-smoke'
Remove-Item -Recurse -Force $workDir -ErrorAction SilentlyContinue
New-Item -ItemType Directory $workDir | Out-Null
$dir = Join-Path $workDir 'FilePickerAPI'
Copy-Item -Recurse $BuildDir $dir
$exe = Join-Path $dir 'FilePickerAPI.exe'
# Файл журнала: путь, который сервис получает через SHGetKnownFolderPath
$serviceLog = Join-Path $env:LOCALAPPDATA 'FilePickerAPI\logs\FilePickerAPI.log'
Remove-Item $serviceLog -ErrorAction SilentlyContinue

function Stop-Exe {
  Get-Process FilePickerAPI -ErrorAction SilentlyContinue | Stop-Process -Force
  Get-Process FilePickerAPI -ErrorAction SilentlyContinue | Wait-Process -Timeout 30
}

function Fail([string]$message) {
  Write-Output "::error::$message"
  Stop-Exe
  exit 1
}

# Вывод не перенаправляется: иначе exe, запущенный bat-скриптом,
# унаследовал бы канал вывода, и чтение ждало бы его выхода. Ввод
# перенаправлен и сразу закрыт, чтобы pause в run_test.bat не ждал
# нажатия клавиши
function Start-Checked([string]$file, [string[]]$arguments) {
  $psi = [Diagnostics.ProcessStartInfo]::new($file)
  foreach ($argument in $arguments) { $psi.ArgumentList.Add($argument) }
  $psi.WorkingDirectory = $workDir
  $psi.UseShellExecute = $false
  $psi.RedirectStandardInput = $true
  $process = [Diagnostics.Process]::Start($psi)
  $process.StandardInput.Close()
  return $process
}

function Wait-Exit($process, [int]$seconds, [string]$what) {
  if (-not $process.WaitForExit($seconds * 1000)) {
    Fail "$what did not exit in $seconds s"
  }
  return $process.ExitCode
}

function Test-Health {
  try {
    $schema = Invoke-RestMethod -Uri $healthUrl -NoProxy -TimeoutSec 2
    return $schema.info.title -eq 'File Picker API'
  } catch {
    return $false
  }
}

function Assert-ServiceLog([string]$text, [string]$what) {
  $content = Get-Content $serviceLog -Raw -Encoding utf8 -ErrorAction SilentlyContinue
  if (-not $content -or -not $content.Contains($text)) {
    Fail "${what}: '$text' not found in $serviceLog"
  }
}

function Wait-Health([int]$seconds) {
  $deadline = (Get-Date).AddSeconds($seconds)
  while ((Get-Date) -lt $deadline) {
    if (Test-Health) { return $true }
    Start-Sleep -Seconds 1
  }
  return $false
}

# 0. Таблица сообщений журнала событий входит в сборку
if (-not (Test-Path (Join-Path $dir 'FilePickerAPI.messages.dll'))) {
  Fail 'FilePickerAPI.messages.dll is missing from the build'
}

# 1. Аргументы, в том числе путь, не принимаются
foreach ($arguments in @(, @('D:\x')) + @(, @('--files-directory', 'D:\x'))) {
  $what = "FilePickerAPI.exe $arguments"
  $code = Wait-Exit (Start-Checked $exe $arguments) 30 $what
  if ($code -ne 2) { Fail "$what exited with $code, expected 2" }
  Write-Output "OK: $what -> 2"
}

# 2. .env читается из папки exe, а FILES_DIRECTORY в нём — ошибка.
# Без перехода в папку exe сервис не увидел бы .env и запустился
$envFile = Join-Path $dir '.env'
Set-Content -Path $envFile -Value 'FILES_DIRECTORY=D:\x' -Encoding utf8NoBOM
$what = 'FilePickerAPI.exe with FILES_DIRECTORY in .env'
$code = Wait-Exit (Start-Checked $exe @()) 60 $what
if ($code -ne 1) { Fail "$what exited with $code, expected 1" }
Assert-ServiceLog 'ERROR app.runner: Ошибка настроек, сервис не запущен' $what
Remove-Item $envFile
Write-Output "OK: $what -> 1"

# 3. run_production.bat: запуск в фоне и проверка ответа
$startupLog = Join-Path $env:LOCALAPPDATA 'FilePickerAPI\startup.log'
Remove-Item $startupLog -ErrorAction SilentlyContinue
$what = 'run_production.bat'
# Сам скрипт ждёт до 30 попыток по 2 с ping и до 2 с curl: запас сверху
$code = Wait-Exit (Start-Checked $cmd @('/c', (Join-Path $dir 'run_production.bat'))) 180 $what
$logLines = @(Get-Content $startupLog -ErrorAction SilentlyContinue)
if ($code -ne 0) { Fail "$what exited with ${code}: $($logLines -join ' ')" }
if ($logLines.Count -ne 1 -or $logLines[0] -notlike 'FilePickerAPI started in daemon mode at *') {
  Fail "$what wrote unexpected startup.log: $($logLines -join ' ')"
}
if (-not (Test-Health)) { Fail "$what reported success, but the service does not respond" }
Assert-ServiceLog 'INFO uvicorn.error: Uvicorn running on http://0.0.0.0:8000' $what
Stop-Exe
# Иначе на шаге 4 мог бы ответить этот экземпляр, а не exe из run_test.bat
if (Test-Health) { Fail "the service still responds after $what instance was stopped" }
Write-Output "OK: $what started the service"

# 4. run_test.bat ждёт exe, пока его не остановят
$what = 'run_test.bat'
$bat = Start-Checked $cmd @('/c', (Join-Path $dir 'run_test.bat'))
if (-not (Wait-Health 60)) { Fail "${what}: the service does not respond" }
if ($bat.HasExited) { Fail "$what exited while the service was running" }
Stop-Exe
# Код выхода не проверяется: после exe скрипт выполняет только pause и
# popd, и код остаётся кодом принудительно остановленного exe. Ветка
# «FilePickerAPI.exe not found» уже исключена: сервис ответил
$null = Wait-Exit $bat 30 "$what after FilePickerAPI.exe stopped"
Write-Output "OK: $what ran the service until it was stopped"
