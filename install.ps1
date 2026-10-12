# Instalador do semaninha (Windows, PowerShell).
#   Botão direito neste arquivo → "Executar com o PowerShell"
#   ou, no PowerShell:  powershell -ExecutionPolicy Bypass -File .\install.ps1
#
# 1. instala o uv (que baixa o Python sozinho)   2. instala o comando `semaninha`
# 3. cria "Semaninha.bat" na Área de Trabalho para abrir com duplo clique.

$ErrorActionPreference = "Stop"
$Source = if ($env:SEMANINHA_SOURCE) { $env:SEMANINHA_SOURCE } else { "git+https://github.com/TryhardCC/semaninha" }

Write-Host "`nInstalando o semaninha" -ForegroundColor Yellow

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "  baixando o uv…"
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}
Write-Host "  ✓ uv $((uv --version).Split(' ')[1])"

$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
if ((Test-Path "$Here\pyproject.toml") -and (Test-Path "$Here\semaninha.py")) { $Source = $Here }
uv tool install --force --quiet --python ">=3.11" $Source
uv tool update-shell | Out-Null
Write-Host "  ✓ comando 'semaninha' instalado"

$Desktop = [Environment]::GetFolderPath("Desktop")
$Launcher = Join-Path $Desktop "Semaninha.bat"
@"
@echo off
chcp 65001 >nul
set PATH=%USERPROFILE%\.local\bin;%PATH%
semaninha
pause
"@ | Set-Content -Path $Launcher -Encoding ASCII
Write-Host "  ✓ atalho criado: $Launcher"

Write-Host "`nPronto! Dê dois cliques em 'Semaninha' na Área de Trabalho, ou abra um terminal novo e digite: semaninha`n"
Write-Host "Na primeira vez ele pede a chave do Last.fm (grátis em https://www.last.fm/api/account/create) e os dados do perfil."
Read-Host "Pressione Enter para fechar"
