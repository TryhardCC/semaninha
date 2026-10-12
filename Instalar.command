#!/bin/zsh
# Duplo clique para instalar o semaninha (macOS).
# Se o macOS reclamar "não pode ser aberto", clique com o botão direito → Abrir.
cd "$(dirname "$0")"
sh ./install.sh
echo
read -r "?Pressione Enter para fechar esta janela…"
