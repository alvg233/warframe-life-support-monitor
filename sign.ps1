param([string]$Target)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Target) { $Target = Join-Path $root "dist\ScreenOcrAlert\ScreenOcrAlert.exe" }

if (-not (Test-Path $Target)) {
    Write-Host "没找到 $Target"
    exit 1
}

$cert = Get-ChildItem Cert:\CurrentUser\My |
    Where-Object { $_.FriendlyName -eq "ScreenOcrAlert local code signing" } |
    Sort-Object NotAfter -Descending | Select-Object -First 1

if (-not $cert) {
    Write-Host "找不到签名证书（自用可选，跳过签名）。"
    exit 0
}

$result = Set-AuthenticodeSignature -FilePath $Target -Certificate $cert
Write-Host ("签名: " + (Split-Path -Leaf $Target) + " -> " + $result.Status)
if ($result.Status -ne "Valid") { exit 3 }
