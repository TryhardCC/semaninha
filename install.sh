#!/bin/sh
# Instalador do semaninha (macOS / Linux).
#
#   sh install.sh                 → instala a partir desta pasta (zip)
#   curl -fsSL <URL>/install.sh | sh   → instala a partir do GitHub (SEMANINHA_SOURCE pode sobrescrever a origem)
#
# O que faz:
#   1. instala o `uv` se não existir (ele baixa o Python sozinho, não precisa de Python no sistema)
#   2. instala o comando `semaninha`
#   3. cria o atalho "Semaninha.command" na Mesa (macOS) ou "semaninha.desktop" (Linux)
#
# Opcional: SEMANINHA_LASTFM_KEY=xxxx sh install.sh   → já deixa a chave do Last.fm salva.

set -eu

GITHUB_SOURCE="${SEMANINHA_SOURCE:-git+https://github.com/TryhardCC/semaninha}"
BIN_DIR="$HOME/.local/bin"
CONFIG_DIR="$HOME/.config/semaninha"

say()  { printf '\n\033[1m%s\033[0m\n' "$1"; }
ok()   { printf '  ✓ %s\n' "$1"; }
warn() { printf '  ! %s\n' "$1"; }

say "🎵  Instalando o semaninha"

# ── 1. uv ────────────────────────────────────────────────────
if ! command -v uv >/dev/null 2>&1 && [ ! -x "$BIN_DIR/uv" ]; then
  printf '  baixando o uv (gerenciador que cuida do Python)…\n'
  curl -LsSf https://astral.sh/uv/install.sh | sh -s -- --quiet
fi
export PATH="$BIN_DIR:$PATH"
command -v uv >/dev/null 2>&1 || { warn "não consegui instalar o uv. Tente de novo ou veja https://docs.astral.sh/uv/"; exit 1; }
ok "uv $(uv --version | cut -d' ' -f2)"

# ── 2. semaninha ─────────────────────────────────────────────
# Se este script está dentro da pasta do projeto (zip), instala dela; senão, do GitHub.
SCRIPT_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd || echo "")"
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/pyproject.toml" ] && [ -f "$SCRIPT_DIR/semaninha.py" ]; then
  SOURCE="$SCRIPT_DIR"
  printf '  instalando a partir da pasta local…\n'
else
  SOURCE="$GITHUB_SOURCE"
  printf '  instalando a partir do GitHub…\n'
fi
uv tool install --force --quiet --python '>=3.11' "$SOURCE"
ok "comando 'semaninha' instalado em $BIN_DIR"

# garante o PATH nos shells (zsh/bash/fish) para quem for usar o Terminal
uv tool update-shell >/dev/null 2>&1 || true

# ── 3. chave opcional ────────────────────────────────────────
if [ -n "${SEMANINHA_LASTFM_KEY:-}" ]; then
  mkdir -p "$CONFIG_DIR"
  if [ ! -f "$CONFIG_DIR/config.json" ]; then
    printf '{\n  "lastfm_api_key": "%s"\n}\n' "$SEMANINHA_LASTFM_KEY" > "$CONFIG_DIR/config.json"
    ok "chave do Last.fm salva"
  fi
fi

# ── 4. atalho na Mesa ────────────────────────────────────────
DESKTOP="$HOME/Desktop"
[ -d "$HOME/Área de Trabalho" ] && DESKTOP="$HOME/Área de Trabalho"
if [ "$(uname)" = "Darwin" ]; then
  LAUNCHER="$DESKTOP/Semaninha.command"
  cat > "$LAUNCHER" <<EOF
#!/bin/zsh
# Abre o semaninha. Pode fechar esta janela quando terminar.
export PATH="\$HOME/.local/bin:\$PATH"
clear
semaninha
EOF
  chmod +x "$LAUNCHER"
  ok "atalho criado: $LAUNCHER"
elif [ -d "$DESKTOP" ]; then
  LAUNCHER="$DESKTOP/semaninha.desktop"
  cat > "$LAUNCHER" <<EOF
[Desktop Entry]
Type=Application
Name=Semaninha
Exec=sh -c 'export PATH="\$HOME/.local/bin:\$PATH"; semaninha'
Terminal=true
Icon=multimedia-player
EOF
  chmod +x "$LAUNCHER"
  ok "atalho criado: $LAUNCHER"
fi

say "Pronto! 🎉"
printf '  • Dê dois cliques em "Semaninha" na Mesa, ou\n'
printf '  • abra um Terminal novo e digite:  semaninha\n\n'
printf '  Na primeira vez ele pede a chave do Last.fm (grátis em https://www.last.fm/api/account/create)\n'
printf '  e os dados do seu perfil: usuário do Last.fm, usuário do RateYourMusic e o CSV exportado do RYM\n'
printf '  (RYM → Settings → Export Data → Export ratings).\n\n'
