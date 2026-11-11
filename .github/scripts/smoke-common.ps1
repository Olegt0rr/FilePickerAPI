# Общие функции дымовых проверок собранного exe: startup-smoke.ps1 и
# msi-smoke.ps1 подключают файл через dot-sourcing. Скрипт PowerShell 7
# (Invoke-WebRequest -NoProxy -SkipHttpErrorCheck и
# ProcessStartInfo.ArgumentList в Windows PowerShell 5.1 нет).

$SmokeBaseUrl = 'http://127.0.0.1:8000'

# GET к сервису: статус и тело; $null, если ответа нет
function Invoke-Http([string]$path) {
  try {
    $response = Invoke-WebRequest -Uri "$SmokeBaseUrl$path" -NoProxy -TimeoutSec 5 -SkipHttpErrorCheck
    return [pscustomobject]@{ Status = [int]$response.StatusCode; Content = [string]$response.Content }
  } catch {
    return $null
  }
}

# На порту отвечает File Picker API. Чужой или обрезанный ответ — не
# ошибка проверки, а «ещё не отвечает»: ожидание продолжается
function Test-Health {
  $response = Invoke-Http '/openapi.json'
  if (-not $response -or $response.Status -ne 200) { return $false }
  try {
    return ($response.Content | ConvertFrom-Json).info.title -eq 'File Picker API'
  } catch {
    return $false
  }
}

# Ждать ответа сервиса не дольше $seconds; $true — ответил
function Wait-HealthFor([int]$seconds) {
  $deadline = (Get-Date).AddSeconds($seconds)
  while ((Get-Date) -lt $deadline) {
    if (Test-Health) { return $true }
    Start-Sleep -Seconds 1
  }
  return $false
}

# Запуск с перенаправленным и сразу закрытым вводом: pause в
# run_test.bat не ждёт нажатия клавиши. Вывод не перенаправляется:
# иначе exe, запущенный bat-скриптом, унаследовал бы канал вывода, и
# чтение ждало бы его выхода
function Start-WithClosedInput([string]$file, [string[]]$arguments, [string]$workDir) {
  $psi = [Diagnostics.ProcessStartInfo]::new($file)
  foreach ($argument in $arguments) { $psi.ArgumentList.Add($argument) }
  $psi.WorkingDirectory = $workDir
  $psi.UseShellExecute = $false
  $psi.RedirectStandardInput = $true
  $process = [Diagnostics.Process]::Start($psi)
  $process.StandardInput.Close()
  return $process
}
