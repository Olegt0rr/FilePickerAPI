# Собирает MSI филиала из папки сборки (installer/FilePickerAPI.wxs) и
# проверяет, что в пакет попали все файлы сборки, кроме bat-скриптов.
#
# Параметры:
#   -Dist    папка сборки Nuitka (FilePickerAPI.exe, FilePickerAPI.messages.dll)
#   -Branch  код филиала: название «File Picker API (филиал <код>)»
#   -Version ProductVersion X.Y.Z (scripts/msi_version.py или имя тега без «v»)
#   -Output  путь к собираемому .msi
#
# WiX и его расширение Util ставятся той версией, что закреплена в
# .config/dotnet-tools.json; скрипт запускается из рабочей копии
# репозитория (нужны .config и installer). Python не нужен: в релизе
# MSI собирает джоб без него.
#
# Используется в build-exe.yml (MSI для дымовой проверки) и в
# release.yml. Скрипт PowerShell 7 (shell: pwsh), как и sign.ps1.

param(
  [Parameter(Mandatory)][string]$Dist,
  [Parameter(Mandatory)][string]$Branch,
  [Parameter(Mandatory)][string]$Version,
  [Parameter(Mandatory)][string]$Output
)

$ErrorActionPreference = 'Stop'

function Fail([string]$message) {
  Write-Output "::error::$message"
  exit 1
}

function Invoke-Native([string]$what, [scriptblock]$command) {
  & $command
  if ($LASTEXITCODE) { Fail "$what failed with exit code $LASTEXITCODE" }
}

if ($Version -notmatch '^[0-9]+\.[0-9]+\.[0-9]+$') { Fail "Version '$Version' is not X.Y.Z" }
if ($Branch -notmatch '^[A-Za-z0-9_-]+$') { Fail "Invalid branch name '$Branch'" }

$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
# WiX требует абсолютный путь к папке сборки: с !(bindpath.…) шаблон
# Files ничего не находит, и WiX только предупреждает (WIX8601)
$Dist = (Resolve-Path $Dist).Path
$Output = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Output)
New-Item -ItemType Directory -Force (Split-Path $Output) | Out-Null

$wixVersion = (Get-Content (Join-Path $root '.config\dotnet-tools.json') -Raw |
  ConvertFrom-Json).tools.wix.version
if (-not $wixVersion) { Fail 'WiX version is not set in .config/dotnet-tools.json' }

Push-Location $root
try {
  Invoke-Native 'dotnet tool restore' { dotnet tool restore }
  # Расширение той же версии, что WiX: новое расширение с WiX 5 несовместимо
  Invoke-Native 'wix extension add' {
    dotnet tool run wix extension add -g "WixToolset.Util.wixext/$wixVersion"
  }
  Invoke-Native 'wix build' {
    dotnet tool run wix build installer\FilePickerAPI.wxs -arch x64 `
      -ext WixToolset.Util.wixext `
      -d "Version=$Version" -d "Branch=$Branch" -d "Dist=$Dist" `
      -o $Output
  }
} finally {
  Pop-Location
}
# Отладочные символы WiX рядом с пакетом не нужны
Remove-Item ([IO.Path]::ChangeExtension($Output, '.wixpdb')) -ErrorAction SilentlyContinue

# Если шаблон Files ничего не нашёл или исключение не сработало, WiX
# собирает пакет с кодом 0. Поэтому сверяем таблицу File пакета с
# папкой сборки: имя и размер каждого файла, кроме bat-скриптов.
# У объектов WindowsInstaller нет библиотеки типов, методы вызываются
# через InvokeMember
function Invoke-Com($object, [string]$name, [Reflection.BindingFlags]$kind, [object[]]$arguments = @()) {
  [System.__ComObject].InvokeMember($name, $kind, $null, $object, $arguments)
}
$method = [Reflection.BindingFlags]::InvokeMethod
$property = [Reflection.BindingFlags]::GetProperty

$installer = New-Object -ComObject WindowsInstaller.Installer
# 0 — msiOpenDatabaseModeReadOnly
$database = Invoke-Com $installer OpenDatabase $method @($Output, 0)
$view = Invoke-Com $database OpenView $method @('SELECT `FileName`, `FileSize` FROM `File`')
[void](Invoke-Com $view Execute $method)
$packaged = [Collections.Generic.List[string]]::new()
while ($record = Invoke-Com $view Fetch $method) {
  # FileName — «КОРОТКОЕ|длинное» или одно имя, если оно уже короткое
  $name = (Invoke-Com $record StringData $property @(1)).Split('|')[-1]
  $size = Invoke-Com $record IntegerData $property @(2)
  $packaged.Add("$name $size")
}
[void](Invoke-Com $view Close $method)
foreach ($object in $record, $view, $database, $installer) {
  if ($object) { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($object) }
}

$expected = @(Get-ChildItem $Dist -Recurse -File | Where-Object Extension -ne '.bat' |
  ForEach-Object { "$($_.Name) $($_.Length)" })
if (-not ($packaged -match '^FilePickerAPI\.exe ')) { Fail "FilePickerAPI.exe is missing from $Output" }
# Сравнение отсортированных списков: имена в подпапках могут повторяться
$packagedSorted = @($packaged | Sort-Object)
$expectedSorted = @($expected | Sort-Object)
if (($packagedSorted -join "`n") -cne ($expectedSorted -join "`n")) {
  Compare-Object $expectedSorted $packagedSorted | ForEach-Object {
    $where = if ($_.SideIndicator -eq '<=') { 'Not in the package' } else { 'Unexpected in the package' }
    Write-Output "::error::${where}: $($_.InputObject)"
  }
  Fail "$Output has $($packaged.Count) files, the build folder $($expected.Count) (without *.bat)"
}
$length = (Get-Item $Output).Length
Write-Output "Built $Output ($Version, branch $Branch): $($packaged.Count) files, $length bytes"
exit 0
