# Проверка пробной службы на Windows-раннере (задачи 1.2–1.4 windows-service).
# Нужны права администратора: служба, источник событий, Program Files.
param(
    [Parameter(Mandatory)][string]$Dist,
    [Parameter(Mandatory)][string]$MessageDll,
    [Parameter(Mandatory)][string]$ResultsDir
)

$ErrorActionPreference = 'Stop'
$name = 'FilePickerProbe'
$installDir = Join-Path $env:ProgramFiles $name
$exe = Join-Path $installDir 'probe.exe'
$dll = Join-Path $installDir "$name.messages.dll"
$sourceKey = "HKLM:\SYSTEM\CurrentControlSet\Services\EventLog\Application\$name"
$expectedStarted = 'Пробная служба запущена: проверка кириллицы ёЁ'
$expectedStopped = 'Пробная служба остановлена'
New-Item -ItemType Directory -Force $ResultsDir | Out-Null
$summary = [System.Collections.Generic.List[string]]::new()

function Assert-True([bool]$condition, [string]$message) {
    if (-not $condition) { throw "PROBE FAILED: $message" }
    $summary.Add("- OK: $message")
    Write-Output "OK: $message"
}

function Get-ProbeEvent([int]$id, [datetime]$since) {
    $deadline = (Get-Date).AddSeconds(20)
    do {
        $found = Get-WinEvent -FilterHashtable @{
            LogName = 'Application'; ProviderName = $name; Id = $id; StartTime = $since
        } -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($found) { return $found }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)
    return $null
}

try {
    # Сборка в Program Files: права наследуются, писать могут только администраторы
    Remove-Item -Recurse -Force $installDir -ErrorAction SilentlyContinue
    Copy-Item -Recurse $Dist $installDir
    Copy-Item $MessageDll $dll

    # 1.4: запуск без аргументов не службой — консольный режим (ошибка 1063)
    $stdout = Join-Path $ResultsDir 'console.json'
    $process = Start-Process -FilePath $exe -NoNewWindow -PassThru `
        -RedirectStandardOutput $stdout -RedirectStandardError (Join-Path $ResultsDir 'console.err')
    Assert-True ($process.WaitForExit(30000)) 'exe без аргументов завершился за 30 секунд'
    if ($process.ExitCode -ne 0) {
        Get-Content -Encoding utf8 (Join-Path $ResultsDir 'console.err') | Write-Output
    }
    Assert-True ($process.ExitCode -eq 0) "exe без аргументов вернул 0 (код $($process.ExitCode))"
    $report = Get-Content -Raw -Encoding utf8 $stdout | ConvertFrom-Json
    Assert-True ($report.mode -eq 'console') 'exe без аргументов перешёл в консольный режим'
    Assert-True ([bool]$report.compiled) 'exe собран Nuitka'
    $summary.Add("- package_preloaded (Nuitka выполнила __init__.py до точки входа): $($report.package_preloaded)")
    $summary.Add("- package_imported_before_entry: $($report.package_imported_before_entry)")
    foreach ($key in 'module_file_name', 'sys_executable', 'sys_argv0') {
        $info = $report.$key
        $summary.Add("- ${key}: $($info.path) (exists: $($info.exists))")
    }
    $summary.Add("- compiled_containing_dir: $($report.compiled_containing_dir)")
    Assert-True ([bool]$report.module_file_name.exists -and (Split-Path -Leaf $report.module_file_name.path) -eq 'probe.exe') 'GetModuleFileName возвращает путь к самому exe'

    # 1.3: источник событий со своим файлом сообщений
    New-Item -Path $sourceKey -Force | Out-Null
    New-ItemProperty -Path $sourceKey -Name EventMessageFile -PropertyType ExpandString -Value $dll -Force | Out-Null
    New-ItemProperty -Path $sourceKey -Name TypesSupported -PropertyType DWord -Value 7 -Force | Out-Null

    # 1.2: служба под виртуальной учётной записью
    & sc.exe create $name binPath= "`"$exe`"" start= demand obj= "NT SERVICE\$name" | Out-Null
    Assert-True ($LASTEXITCODE -eq 0) 'sc.exe create с NT SERVICE\FilePickerProbe'
    & sc.exe sidtype $name unrestricted | Out-Null
    Assert-True ($LASTEXITCODE -eq 0) 'sc.exe sidtype unrestricted'

    $since = (Get-Date).AddSeconds(-2)
    Start-Service $name
    (Get-Service $name).WaitForStatus('Running', '00:00:30')
    Assert-True ((Get-Service $name).Status -eq 'Running') 'служба запустилась'

    $started = Get-ProbeEvent 1000 $since
    Assert-True ($null -ne $started) 'событие 1000 записано'
    Set-Content -Encoding utf8 (Join-Path $ResultsDir 'event-1000.txt') $started.Message
    Assert-True ($started.Message -eq $expectedStarted) "текст события 1000 совпал с кириллицей: '$($started.Message)'"
    Assert-True ($started.Level -eq 4) 'уровень события 1000 — сведения'

    Stop-Service $name
    (Get-Service $name).WaitForStatus('Stopped', '00:00:30')
    Assert-True ((Get-Service $name).Status -eq 'Stopped') 'служба остановилась за 30 секунд'
    Start-Sleep -Seconds 2
    $left = Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $exe }
    Assert-True ($null -eq $left) 'процесса службы не осталось'

    $stopped = Get-ProbeEvent 1001 $since
    Assert-True ($null -ne $stopped -and $stopped.Message -eq $expectedStopped) 'событие 1001 записано с полным текстом'

    # Тот же текст, что показывает Event Viewer, через wevtutil
    $query = "*[System[Provider[@Name='$name']]]"
    $text = & wevtutil.exe qe Application "/q:$query" /f:text /c:4 /rd:true | Out-String
    Set-Content -Encoding utf8 (Join-Path $ResultsDir 'wevtutil.txt') $text
    Assert-True ($LASTEXITCODE -eq 0) 'wevtutil qe отработал'
    Assert-True ($text -notmatch 'description for Event ID|cannot be found') 'wevtutil показывает текст без «описание не найдено»'
}
finally {
    & sc.exe stop $name 2>&1 | Out-Null
    & sc.exe delete $name 2>&1 | Out-Null
    Remove-Item -Path $sourceKey -Recurse -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    Remove-Item -Recurse -Force $installDir -ErrorAction SilentlyContinue
    if ($env:GITHUB_STEP_SUMMARY) {
        "## Пробная служба`n" + ($summary -join "`n") | Add-Content -Encoding utf8 $env:GITHUB_STEP_SUMMARY
    }
}
