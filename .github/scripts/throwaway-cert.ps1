# Создаёт одноразовый самоподписанный сертификат подписи кода и кладёт
# его в переменные окружения процесса, которые читает sign.ps1:
#   CERT_PFX_BASE64 — PFX в base64, CERT_PASSWORD — случайный пароль.
# Переменные окружения общие для процесса pwsh, поэтому достаточно
# вызвать скрипт в том же шаге перед sign.ps1. В GITHUB_ENV ключ не
# пишется.
#
# Используется в build-exe.yml: джоб sign-smoke и подпись копий сборки и
# MSI для дымовой проверки. Боевой сертификат доступен только релизным
# тегам, а логику подписи нужно проверять в каждом PR.

param([string]$Subject = 'CN=FilePickerAPI CI smoke test')

$ErrorActionPreference = 'Stop'

$rsa = [System.Security.Cryptography.RSA]::Create(3072)
$request = [System.Security.Cryptography.X509Certificates.CertificateRequest]::new(
  $Subject, $rsa, 'SHA256',
  [System.Security.Cryptography.RSASignaturePadding]::Pkcs1)
$eku = [System.Security.Cryptography.OidCollection]::new()
[void]$eku.Add([System.Security.Cryptography.Oid]::new('1.3.6.1.5.5.7.3.3'))
$request.CertificateExtensions.Add(
  [System.Security.Cryptography.X509Certificates.X509EnhancedKeyUsageExtension]::new($eku, $false))
$request.CertificateExtensions.Add(
  [System.Security.Cryptography.X509Certificates.X509KeyUsageExtension]::new('DigitalSignature', $true))
$cert = $request.CreateSelfSigned([DateTimeOffset]::UtcNow.AddDays(-1), [DateTimeOffset]::UtcNow.AddDays(1))

$env:CERT_PASSWORD = [guid]::NewGuid().ToString()
$env:CERT_PFX_BASE64 = [Convert]::ToBase64String($cert.Export('Pfx', $env:CERT_PASSWORD))
