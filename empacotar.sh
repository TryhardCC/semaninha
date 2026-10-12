#!/bin/sh
# Gera dist/semaninha.zip pronto para mandar para alguém.
# Entra só o necessário: código, instaladores e README. Ficam de fora CSVs de ratings,
# imagens, venvs e qualquer chave de API (a chave fica no config de cada pessoa).

set -eu
cd "$(dirname "$0")"
mkdir -p dist
STAGE="$(mktemp -d)/semaninha"
mkdir -p "$STAGE"
cp semaninha.py weekly_music_grid.py find_cjk_font.py pyproject.toml requirements.txt \
   install.sh install.ps1 Instalar.command README.md "$STAGE"/
chmod +x "$STAGE/install.sh" "$STAGE/Instalar.command"
rm -f dist/semaninha.zip
(cd "$(dirname "$STAGE")" && zip -qr "$OLDPWD/dist/semaninha.zip" semaninha -x '*.DS_Store')
rm -rf "$(dirname "$STAGE")"
echo "ok  dist/semaninha.zip ($(du -h dist/semaninha.zip | cut -f1))"
echo "    Quem receber: descompacta e dá dois cliques em Instalar.command (macOS)"
echo "    ou botão direito → Executar com o PowerShell em install.ps1 (Windows)."
