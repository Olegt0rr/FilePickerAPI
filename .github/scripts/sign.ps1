# Подписывает все неподписанные PE-файлы (exe, dll, pyd) в папке сборки
# сертификатом Authenticode и проверяет результат.
#
# Входные данные берутся из переменных окружения:
#   CERT_PFX_BASE64 — PFX сертификата подписи кода в base64
#   CERT_PASSWORD   — пароль от PFX
#   TIMESTAMP_URL   — RFC 3161 сервер меток времени
#   SIGN_PATH       — папка со сборками (по умолчанию dist: сборки всех
#                     филиалов, dist/FilePickerAPI-<филиал>/FilePickerAPI)
#
# Используется в release.yml (боевой сертификат) и в build-exe.yml
# (одноразовый сертификат, чтобы проверять логику подписи в каждом PR).

$ErrorActionPreference = 'Stop'
$signPath = if ($env:SIGN_PATH) { $env:SIGN_PATH } else { 'dist' }

if (-not $env:CERT_PFX_BASE64 -or -not $env:CERT_PASSWORD) {
  Write-Output "::error::Signing secrets are not set in the code-signing environment"
  exit 1
}

# Ключ не импортируем в хранилище сертификатов, чтобы он не остался
# в профиле раннера: signtool читает временный PFX, который сразу
# удаляется.
$pfxBytes = [Convert]::FromBase64String($env:CERT_PFX_BASE64)
$pfxCerts = [System.Security.Cryptography.X509Certificates.X509Certificate2Collection]::new()
$pfxCerts.Import(
  $pfxBytes,
  $env:CERT_PASSWORD,
  [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet)
$signingCert = $pfxCerts | Where-Object { $_.HasPrivateKey } | Select-Object -First 1
if (-not $signingCert) {
  Write-Output "::error::PFX does not contain a certificate with a private key"
  exit 1
}
$thumbprint = $signingCert.Thumbprint

$signtool = Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin\*\x64\signtool.exe" |
  Sort-Object FullName -Descending | Select-Object -First 1 -ExpandProperty FullName

$binaries = Get-ChildItem $signPath -Recurse -File -Include *.exe, *.dll, *.pyd
$status = @{}
$binaries | ForEach-Object { $status[$_.FullName] = (Get-AuthenticodeSignature $_.FullName).Status }

# Подписываем только файлы без подписи. Чужая подпись в любом другом
# состоянии, кроме Valid (HashMismatch, NotTrusted, UnknownError...),
# означает изменённый или подозрительный файл: свою подпись поверх
# не ставим, иначе проблема станет незаметной, и останавливаем релиз.
$suspicious = $binaries | Where-Object { $status[$_.FullName] -notin 'Valid', 'NotSigned' }
if ($suspicious) {
  $suspicious | ForEach-Object { Write-Output "::error::Signature status $($status[$_.FullName]): $($_.FullName)" }
  exit 1
}

$unsigned = $binaries | Where-Object { $status[$_.FullName] -eq 'NotSigned' }

if ($unsigned) {
  $pfx = Join-Path $env:RUNNER_TEMP 'signing.pfx'
  [IO.File]::WriteAllBytes($pfx, $pfxBytes)
  try {
    # Пачками: файлы сборок всех филиалов в одной команде упёрлись бы
    # в лимит длины командной строки Windows (32767 символов).
    $paths = @($unsigned.FullName)
    $batchSize = 50
    for ($i = 0; $i -lt $paths.Count; $i += $batchSize) {
      $batch = $paths[$i..([Math]::Min($i + $batchSize, $paths.Count) - 1)]
      & $signtool sign /f $pfx /p $env:CERT_PASSWORD /fd SHA256 /tr $env:TIMESTAMP_URL /td SHA256 `
        /d FilePickerAPI $batch
      if ($LASTEXITCODE) { exit $LASTEXITCODE }
    }
  } finally {
    Remove-Item $pfx -Force
  }
}

# Проверяем подписи полноценно: каждый PE-файл обязан иметь статус
# Valid, а подписанные нами — ещё и наш отпечаток. Раннер не доверяет
# нашему корню (самоподписанный сертификат или внутренний CA), и
# Windows вернула бы UnknownError даже для верной подписи. Поэтому на
# время проверки добавляем корневые сертификаты из PFX (только
# открытую часть) в доверенные корни раннера и потом убираем.
$roots = @($pfxCerts | Where-Object { $_.Subject -eq $_.Issuer } |
  ForEach-Object { [System.Security.Cryptography.X509Certificates.X509Certificate2]::new($_.RawData) })
$rootStore = [System.Security.Cryptography.X509Certificates.X509Store]::new('Root', 'LocalMachine')
$rootStore.Open('ReadWrite')
$roots | ForEach-Object { $rootStore.Add($_) }
try {
  $signedByUs = @($unsigned | ForEach-Object { $_.FullName })
  $bad = $binaries | Where-Object {
    $sig = Get-AuthenticodeSignature $_.FullName
    $sig.Status -ne 'Valid' -or
      ($_.FullName -in $signedByUs -and $sig.SignerCertificate.Thumbprint -ne $thumbprint)
  }
  if ($bad) {
    $bad | ForEach-Object {
      $sig = Get-AuthenticodeSignature $_.FullName
      Write-Output "::error::Signature check failed ($($sig.Status): $($sig.StatusMessage)): $($_.FullName)"
    }
    exit 1
  }
} finally {
  $roots | ForEach-Object { $rootStore.Remove($_) }
  $rootStore.Close()
}
Write-Output "Signed $(@($unsigned).Count) of $($binaries.Count) binaries with $thumbprint"

# Файлы с чужой действительной подписью оставлены как есть: их
# издателей нужно разрешить в политике WDAC/AppLocker.
$binaries | Where-Object { $status[$_.FullName] -eq 'Valid' } |
  Group-Object { (Get-AuthenticodeSignature $_.FullName).SignerCertificate.Subject } |
  ForEach-Object { Write-Output "Kept third-party signature on $($_.Count) file(s): $($_.Name)" }
