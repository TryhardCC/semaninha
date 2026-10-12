# Instalador do semaninha (Windows, PowerShell). Sem acentos de proposito: o PowerShell
# antigo (5.1) le arquivos sem BOM como ANSI e embaralha caracteres acentuados.
#
#   Pela internet (CMD ou PowerShell):
#     powershell -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/TryhardCC/semaninha/main/install.ps1 | iex"
#   Pelo zip: botao direito neste arquivo -> "Executar com o PowerShell"
#
# 1. instala o uv (que baixa o Python sozinho)   2. instala o comando `semaninha`
# 3. cria "Semaninha.bat" na Area de Trabalho para abrir com duplo clique.

$ErrorActionPreference = "Stop"
$Source = if ($env:SEMANINHA_SOURCE) { $env:SEMANINHA_SOURCE } else { "https://github.com/TryhardCC/semaninha/archive/refs/heads/main.zip" }
$BinDir = Join-Path $env:USERPROFILE ".local\bin"

Write-Host ""
Write-Host "Instalando o semaninha" -ForegroundColor Yellow

# -- 1. uv ------------------------------------------------------------
$env:Path = "$BinDir;$env:Path"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "  baixando o uv (gerenciador que cuida do Python)..."
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $env:Path = "$BinDir;$env:Path"
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "  ! nao consegui instalar o uv. Veja https://docs.astral.sh/uv/" -ForegroundColor Red
    exit 1
}
Write-Host "  ok uv $((uv --version).Split(' ')[1])"

# -- 2. semaninha -----------------------------------------------------
# Se este script esta dentro da pasta do projeto (zip), instala dela; senao, do GitHub.
$Here = $null
if ($MyInvocation.MyCommand.Path) { $Here = Split-Path -Parent $MyInvocation.MyCommand.Path }
if ($Here -and (Test-Path "$Here\pyproject.toml") -and (Test-Path "$Here\semaninha.py")) {
    $Source = $Here
    Write-Host "  instalando a partir da pasta local..."
} else {
    Write-Host "  instalando a partir do GitHub..."
}
uv tool install --force --reinstall --quiet --python ">=3.11" $Source
if ($LASTEXITCODE -ne 0) { Write-Host "  ! a instalacao falhou" -ForegroundColor Red; exit 1 }
Write-Host "  ok comando 'semaninha' instalado em $BinDir"

# PATH do usuario (para digitar `semaninha` num terminal novo)
try { uv tool update-shell 2>$null | Out-Null } catch {}
$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not $UserPath) { $UserPath = "" }
if ($UserPath -notlike "*$BinDir*") {
    [Environment]::SetEnvironmentVariable("Path", "$BinDir;$UserPath", "User")
    Write-Host "  ok pasta $BinDir adicionada ao PATH"
}

# -- 3. atalho na Area de Trabalho ------------------------------------
$Desktop = [Environment]::GetFolderPath("Desktop")
$Launcher = Join-Path $Desktop "Semaninha.bat"
# Chama o .exe pelo caminho completo: um `semaninha` solto resolveria para este proprio
# Semaninha.bat (o cmd procura na pasta atual primeiro) e entraria em recursao.
@"
@echo off
chcp 65001 >nul
cd /d "%USERPROFILE%"
set "SEMANINHA_EXE=%USERPROFILE%\.local\bin\semaninha.exe"
if not exist "%SEMANINHA_EXE%" (
  echo Nao encontrei %SEMANINHA_EXE%
  echo Rode o instalador de novo: powershell -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/TryhardCC/semaninha/main/install.ps1 | iex"
  echo.
  pause
  exit /b 1
)
"%SEMANINHA_EXE%"
echo.
pause
"@ | Set-Content -Path $Launcher -Encoding ASCII
Write-Host "  ok atalho criado: $Launcher"

Write-Host ""
Write-Host "Pronto!" -ForegroundColor Yellow
Write-Host "  - De dois cliques em 'Semaninha' na Area de Trabalho, ou"
Write-Host "  - abra um terminal NOVO (feche o atual) e digite:  semaninha"
Write-Host ""
Write-Host "  Na primeira vez ele pede a chave do Last.fm (gratis em https://www.last.fm/api/account/create)"
Write-Host "  e os dados do seu perfil: usuario do Last.fm e o CSV exportado do RYM"
Write-Host "  (RYM -> Settings -> Export Data -> Export ratings)."
Write-Host ""
if ($Host.Name -eq "ConsoleHost") { Read-Host "Pressione Enter para fechar" | Out-Null }
