#!/usr/bin/env python3
"""
find_cjk_font.py
────────────────
Diagnóstico de fontes CJK: testa quais fontes instaladas no sistema
conseguem renderizar japonês (hiragana, katakana, kanji) corretamente.

Uso:
    python find_cjk_font.py

Saída:
    - Lista de fontes testadas com ✅ (funciona) ou ❌ (quadradinhos)
    - Imagem cjk_font_test.png com amostra visual de cada fonte
    - Instrução de qual caminho colocar em CJK_FONT_PATH no script principal
"""

import glob
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("Pillow não encontrado. Instale com:  pip install Pillow")
    sys.exit(1)

TEST_TEXT = "ミドリ  あらためまして  漢字テスト"
FONT_SIZE = 16

# ── candidatos a testar ──────────────────────────────────────

candidates: list[str] = []

# macOS — Hiragino (fontes japonesas do sistema Apple)
for pattern in [
    "/System/Library/Fonts/*ゴシック*.ttc",
    "/System/Library/Fonts/*ゴシック*.otf",
    "/System/Library/Fonts/Supplemental/*ゴシック*.ttc",
    "/System/Library/Fonts/Supplemental/*ゴシック*.otf",
    "/Library/Fonts/*ゴシック*.ttc",
    "/Library/Fonts/*明朝*.otf",
]:
    candidates.extend(sorted(glob.glob(pattern)))

# macOS — CJK genérico
candidates += [
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Supplemental/Hiragino Sans GB W3.ttc",
    "/Library/Fonts/Arial Unicode MS.ttf",
]

# Homebrew / Noto
for pattern in [
    "/opt/homebrew/share/fonts/**/*Noto*CJK*.otf",
    "/opt/homebrew/share/fonts/**/*Noto*CJK*.ttc",
    "/usr/local/share/fonts/*Noto*CJK*.otf",
    "/Library/Fonts/Noto*CJK*.otf",
    "/Library/Fonts/Noto*CJK*.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKjp-Regular.otf",
]:
    if "*" in pattern:
        candidates.extend(sorted(glob.glob(pattern, recursive=True)))
    else:
        candidates.append(pattern)

# Windows
candidates += [
    "C:/Windows/Fonts/meiryo.ttc",
    "C:/Windows/Fonts/msgothic.ttc",
    "C:/Windows/Fonts/yugothic.ttc",
]

# ── função de teste ──────────────────────────────────────────

def font_renders_japanese(font: ImageFont.FreeTypeFont) -> bool:
    """
    Compara 'あ' (hiragana) com 'ア' (katakana).
    Fonte sem suporte: ambos viram o mesmo □ → iguais → False.
    Fonte com suporte: caracteres distintos → pixels diferentes → True.
    """
    size = 24
    def render(ch):
        img = Image.new("L", (size, size), 255)
        ImageDraw.Draw(img).text((0, 0), ch, fill=0, font=font)
        return bytes(img.tobytes())
    return render("あ") != render("ア")

# ── testa cada candidato ─────────────────────────────────────

print(f"\nTestando fontes para renderizar: {TEST_TEXT}\n")

working:  list[tuple[str, ImageFont.FreeTypeFont]] = []
broken:   list[str] = []
missing:  list[str] = []

for path in dict.fromkeys(candidates):   # deduplica mantendo ordem
    if not Path(path).exists():
        missing.append(path)
        continue
    try:
        font = ImageFont.truetype(path, FONT_SIZE)
        ok   = font_renders_japanese(font)
        if ok:
            working.append((path, font))
            print(f"  ✅  {path}")
        else:
            broken.append(path)
            print(f"  ❌  {path}  (carregou mas não tem glyphs japoneses)")
    except Exception as e:
        broken.append(path)
        print(f"  ❌  {path}  (erro: {e})")

print(f"\n  {len(missing)} caminhos não encontrados no sistema (normal)")

# ── gera imagem de amostra visual ────────────────────────────

if working:
    row_h  = FONT_SIZE + 14
    pad    = 10
    width  = 780
    height = len(working) * row_h + pad * 2

    img  = Image.new("RGB", (width, height), (20, 20, 20))
    draw = ImageDraw.Draw(img)

    try:
        label_font = ImageFont.truetype(
            "/System/Library/Fonts/Helvetica.ttc", 11
        )
    except Exception:
        label_font = ImageFont.load_default()

    for i, (path, font) in enumerate(working):
        y      = pad + i * row_h
        name   = Path(path).name
        label  = f"{name[:40]:<40}  "
        draw.text((pad, y), label, fill=(120, 120, 120), font=label_font)
        draw.text((pad + 340, y), TEST_TEXT, fill=(240, 240, 240), font=font)

    out = "cjk_font_test.png"
    img.save(out)
    print(f"\n  Amostra visual salva em: {out}")
else:
    print("\n  ⚠️  Nenhuma fonte funcional encontrada.")

# ── recomendação ─────────────────────────────────────────────

print()
if working:
    best = working[0][0]
    print("=" * 60)
    print(f"  Fonte recomendada: {best}")
    print()
    print("  Adicione ao weekly_music_grid.py:")
    print(f'    CJK_FONT_PATH = "{best}"')
    print("=" * 60)
else:
    print("=" * 60)
    print("  Nenhuma fonte CJK encontrada. Para instalar:")
    print()
    print("  macOS (Homebrew):")
    print("    brew install --cask font-noto-sans-cjk-japanese")
    print("  Depois adicione ao weekly_music_grid.py:")
    print('    CJK_FONT_PATH = "/Library/Fonts/NotoSansCJKjp-Regular.otf"')
    print()
    print("  Linux:")
    print("    sudo apt install fonts-noto-cjk")
    print("=" * 60)

print()
