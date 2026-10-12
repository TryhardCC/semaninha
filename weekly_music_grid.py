#!/usr/bin/env python3
"""
weekly_music_grid.py
────────────────────
Gera uma grade PNG dos seus álbuns mais ouvidos na semana,
com ratings do Rate Your Music sobrepostos nas capas.

Modos disponíveis (configurar MODE abaixo):
  "scrobbles"     — top N álbuns por número de scrobbles (padrão)
  "listening_time"— top N álbuns por tempo total de escuta
                    (duration × playcount de cada faixa)

Uso:
    semaninha                      # CLI amigável (ver semaninha_cli.py / README)
    python weekly_music_grid.py    # uso direto, com as flags abaixo (--help)

Requisitos:
    pip install requests Pillow

Configuração:
    Edite a seção CONFIG abaixo antes de rodar.
    Para o RYM, exporte seus ratings em: Settings → Export Data
"""

import argparse
import csv
import datetime
import glob
import html as _html_lib
import io
import json
import math
import os
import re
import sys
import time as _time
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

# ═══════════════════════════════════════════════════════════
#  CONFIG — edite aqui antes de rodar
# ═══════════════════════════════════════════════════════════

LASTFM_API_KEY  = os.environ.get("LASTFM_API_KEY", "")   # a CLI `semaninha` passa a chave do config;
                                                          # uso direto: exporte LASTFM_API_KEY (last.fm/api)

# Os valores abaixo são apenas DEFAULTS — podem ser sobrescritos via argumentos de
# linha de comando (--username, --csv, --output, --period, --mode). Veja `--help`.
LASTFM_USERNAME = "grugges"              # username no Last.fm         (--username)
RYM_CSV_PATH    = "rym_export_lise.csv"  # CSV exportado do RYM        (--csv)
OUTPUT_PATH     = "weekly_grid"          # prefixo do arquivo de saída (--output); datetime + .png são adicionados

# ── período de análise ───────────────────────────────────────
# "week"  → últimos 7 dias,  grid 5×5 (top 25)
# "month" → últimos 30 dias, grid 8×8 (top 64)
PERIOD = "week"

_PERIOD_PRESETS = {
    "week":  {"days": 7,  "lastfm": "7day",   "top_n": 25, "columns": 5},
    "month": {"days": 30, "lastfm": "1month", "top_n": 64, "columns": 8},
}
if PERIOD not in _PERIOD_PRESETS:
    raise SystemExit(f"PERIOD inválido: {PERIOD!r}. Use 'week' ou 'month'.")
_preset = _PERIOD_PRESETS[PERIOD]

TOP_N         = _preset["top_n"]      # quantos álbuns exibir (= COLUMNS²)
COLUMNS       = _preset["columns"]    # número de colunas na grid
PERIOD_DAYS   = _preset["days"]       # janela em dias (modo listening_time)
LASTFM_PERIOD = _preset["lastfm"]     # período da Last.fm API (modo scrobbles)
WINDOW_FROM: int | None = None        # janela explícita (--since/--until), epoch; None = últimos PERIOD_DAYS
WINDOW_TO:   int | None = None

# ── modo de ordenação ────────────────────────────────────────
# "scrobbles"      → top N álbuns por número de scrobbles
# "listening_time" → top N álbuns por tempo total de escuta
MODE = "listening_time"

CJK_FONT_PATH = ""   # a CLI passa a fonte do config; vazio = auto-detectar

# (só para o modo listening_time)
# Duração padrão em segundos usada quando a faixa não tem duração no Last.fm
DEFAULT_TRACK_DURATION = 210   # 3 min 30 s
# Máximo de workers paralelos para buscar info das faixas
TRACK_INFO_WORKERS = 5
# Exibir o tempo ouvido (ex: "1h23m") no overlay inferior da capa?
# False → visual idêntico ao modo scrobbles (só artista – álbum)
SHOW_TIME_LABEL = False

# ── artistas ignorados ──────────────────────────────────────
# Álbuns desses artistas não aparecem na grid (case-sensitive, igual ao Last.fm)
IGNORED_ARTISTS: set[str] = {
    "Cryptopsy"
}

# ── álbuns ignorados ────────────────────────────────────────
# Álbuns com esses títulos não aparecem na grid (case-sensitive, igual ao Last.fm)
# Útil para remover músicas soltas que aparecem como álbum no modo listening_time
IGNORED_ALBUMS: set[str] = {
    "Lover, You Should've Come Over",
}

# ── pares (artista, álbum) ignorados ────────────────────────
# Remove apenas a combinação exata de artista + álbum (case-sensitive, igual ao Last.fm)
# Útil quando dois artistas têm um álbum com o mesmo nome (ex: splits)
IGNORED_ARTIST_ALBUMS: set[tuple[str, str]] = {
    ("Corrupted", "Noothgrush / Corrupted Split"),
}

# ── fonte CJK (japonês, chinês, coreano) ────────────────────
# Deixe vazio para auto-detectar. Se caracteres aparecerem como □,
# aponte para um arquivo .ttf/.ttc com suporte a CJK, por exemplo:
#   CJK_FONT_PATH = "/Library/Fonts/NotoSansCJKjp-Regular.otf"
# Para instalar via Homebrew:  brew install --cask font-noto-sans-cjk-japanese
# (o valor fica definido mais acima, logo após MODE)

# ── visual ──────────────────────────────────────────────────
COVER_SIZE     = 280    # tamanho das capas em px (sem gaps — as capas se tocam)
OVERLAY_MARGIN = 8      # distância do overlay às bordas da capa
OVERLAY_ALPHA  = 185    # opacidade do fundo dos overlays (0–255)
OVERLAY_PAD_X  = 10     # padding horizontal interno dos overlays
OVERLAY_PAD_Y  = 5      # padding vertical interno dos overlays
STAR_SIZE      = 14     # raio externo de cada estrela em px

BG_COLOR   = (13, 13, 13)
TEXT_COLOR = (255, 255, 255)
STAR_COLOR = (255, 195, 50)

# ═══════════════════════════════════════════════════════════


# ── helpers ─────────────────────────────────────────────────

def normalize(s: str) -> str:
    """Normaliza string para matching fuzzy (sem acentos, lowercase, só alfanum)."""
    s = unicodedata.normalize("NFKD", s.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return "".join(c for c in s if c.isalnum() or c.isspace()).strip()


def truncate(text: str, max_len: int) -> str:
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


# ── matching helpers ─────────────────────────────────────────

def _unescape(s: str) -> str:
    """Decodifica HTML entities (ex: &amp; → &)."""
    return _html_lib.unescape(s).strip()


def _strip_the(s: str) -> str:
    """Remove 'The ' prefix (case-insensitive)."""
    return re.sub(r"(?i)^the\s+", "", s).strip()


def _strip_feat(s: str) -> str:
    """
    Remove sufixos de colaboração do nome do artista.
    Cobre: '& X', '+ X', 'feat. X', 'ft. X', 'with X', 'featuring X'
    """
    s = re.sub(r"\s*[&+]\s*.+$", "", s)
    s = re.sub(r"(?i)\s*(?:feat\.?|ft\.?|featuring|with)\s+.+$", "", s)
    return s.strip()


def _strip_edition(s: str) -> str:
    """
    Remove qualificadores de edição/remasterização de títulos de álbuns.
    Ex: '(2019 Remaster)', '[Deluxe Edition]', '- Remastered',
        '(1994 Remaster'  ← parêntese não fechado (Last.fm às vezes trunca)
    """
    s = re.sub(
        r"(?i)\s*[\(\[]\s*(?:\d{4}\s+)?"
        r"(?:remaster(?:ed)?|deluxe|expanded|special|anniversary|"
        r"edition|version|re.?issue|bonus|complete|collector)[^\)\]]*[\)\]]?",
        # ↑ [\)\]]? — fechar parêntese é OPCIONAL para cobrir títulos truncados
        "", s,
    )
    s = re.sub(
        r"(?i)\s*[-–]\s*(?:\d{4}\s+)?(?:remaster(?:ed)?|deluxe|expanded|special|anniversary).*$",
        "", s,
    )
    return s.strip()


def _artist_variants(artist: str) -> list[str]:
    """
    Gera variantes normalizadas do nome do artista para matching multi-estratégia.
    Ordem: mais específico → mais genérico.
    """
    base = _unescape(artist)
    seen: list[str] = []
    for candidate in [
        base,                           # original
        _strip_the(base),               # sem "The"
        _strip_feat(base),              # sem feat/collab
        _strip_the(_strip_feat(base)),  # sem "The" + sem feat
    ]:
        n = normalize(candidate)
        if n and n not in seen:
            seen.append(n)
    return seen


def _title_variants(title: str) -> list[str]:
    """
    Gera variantes normalizadas do título do álbum para matching multi-estratégia.
    Ordem: mais específico → mais genérico.
    """
    base = _unescape(title)
    seen: list[str] = []
    for candidate in [base, _strip_edition(base)]:
        n = normalize(candidate)
        if n and n not in seen:
            seen.append(n)
    return seen


def rating_to_stars(raw: str) -> str | None:
    """
    Converte rating do RYM para string de estrelas (escala 0.5–5).
    O CSV do RYM exporta na escala 1–10, então dividimos por 2.
    Ex: '9' → 4.5 → '★★★★½'  |  '10' → 5 → '★★★★★'  |  '6' → 3 → '★★★'
    """
    try:
        val = float(raw)
        if val <= 0:
            return None
        # CSV do RYM usa escala 1-10; converte para 0.5-5
        val = val / 2
        full = int(val)
        half = (val % 1) >= 0.25
        return "★" * full + ("½" if half else "")
    except (ValueError, TypeError):
        return str(raw).strip() or None


def _fetch_limit() -> int:
    """Returns a fetch limit with buffer to account for IGNORED_ARTISTS/IGNORED_ALBUMS filtering."""
    return TOP_N + len(IGNORED_ARTISTS) + len(IGNORED_ALBUMS) + 10


# ── Last.fm ──────────────────────────────────────────────────

class LastfmError(RuntimeError):
    """Erro devolvido pela API do Last.fm (usuário inexistente, chave inválida…)."""


def get_weekly_top_albums(api_key: str, username: str, limit: int = TOP_N) -> list[dict]:
    """Busca os álbuns mais ouvidos no período configurado via Last.fm API."""
    print(f"  Buscando top {limit} álbuns de @{username} (últimos {PERIOD_DAYS} dias)…")
    resp = requests.get(
        "https://ws.audioscrobbler.com/2.0/",
        params={
            "method":  "user.getTopAlbums",
            "user":    username,
            "period":  LASTFM_PERIOD,
            "limit":   limit,
            "api_key": api_key,
            "format":  "json",
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    if "error" in data:
        raise LastfmError(str(data.get("message", data["error"])))

    albums = data.get("topalbums", {}).get("album", [])
    result = []
    for a in albums:
        images = {img["size"]: img["#text"] for img in a.get("image", [])}
        cover_url = (
            images.get("extralarge")
            or images.get("large")
            or images.get("medium")
            or ""
        )
        result.append(
            {
                "name":      a["name"],
                "artist":    a["artist"]["name"],
                "plays":     int(a.get("playcount", 0)),
                "cover_url": cover_url,
            }
        )
    return result


def _lastfm_get(api_key: str, params: dict, timeout: int = 10) -> dict:
    """Wrapper para chamadas à Last.fm API."""
    p = {"api_key": api_key, "format": "json", **params}
    resp = requests.get("https://ws.audioscrobbler.com/2.0/", params=p, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _fetch_album_cover(api_key: str, artist: str, album: str) -> str:
    """
    Busca a URL da capa de um álbum via album.getInfo.
    Retorna string vazia se não encontrar.
    """
    try:
        data = _lastfm_get(api_key, {
            "method":      "album.getInfo",
            "artist":      artist,
            "album":       album,
            "autocorrect": 1,
        })
        if "error" in data:
            return ""
        images = {img["size"]: img["#text"] for img in data.get("album", {}).get("image", [])}
        return _best_image(images)
    except Exception:
        return ""


def _fetch_track_info(api_key: str, artist: str, track: str) -> dict | None:
    """
    Busca info de uma faixa no Last.fm (álbum + duração).
    Retorna None se não encontrar.
    """
    try:
        data = _lastfm_get(api_key, {
            "method":  "track.getInfo",
            "artist":  artist,
            "track":   track,
            "autocorrect": 1,
        })
        if "error" in data:
            return None
        return data.get("track")
    except Exception:
        return None


_LASTFM_NO_IMAGE = "2a96cbd8b46e442fc41c2b86b821562f"   # hash do placeholder vazio


def _best_image(images: dict) -> str:
    """Escolhe a melhor URL de imagem, ignorando o placeholder vazio do Last.fm."""
    for size in ("extralarge", "large", "medium"):
        url = images.get(size, "")
        if url and _LASTFM_NO_IMAGE not in url:
            return url
    return ""


def get_top_albums_in_window(
    api_key:  str,
    username: str,
    limit:    int = TOP_N,
    rank_by:  str = "time",
) -> list[dict]:
    """
    Top álbuns dentro da janela configurada (WINDOW_FROM/WINDOW_TO ou últimos PERIOD_DAYS).
    rank_by="time"  → por tempo total de escuta (duration × plays)   [modo listening_time]
    rank_by="plays" → por número de scrobbles                          [modo scrobbles]

    Estratégia:
      1. user.getRecentTracks (paginado, janela configurada) → scrobbles com álbum
         já embutido em cada entrada, sem ambiguidade de "qual álbum é esse".
      2. Conta plays por faixa única; descobre duração via track.getInfo em paralelo
         (só para faixas únicas, não para todos os scrobbles).
      3. Agrupa por álbum, soma duration × plays, retorna top N.
    """
    # 1. Coleta todos os scrobbles do período (paginado)
    from_time, now = window_bounds()
    print(f"  [{rank_by}] Buscando scrobbles de @{username} "
          f"({datetime.datetime.fromtimestamp(from_time):%Y-%m-%d %H:%M} → {datetime.datetime.fromtimestamp(now):%Y-%m-%d %H:%M})…")
    scrobbles: list[dict] = []
    page = 1

    while True:
        data = _lastfm_get(api_key, {
            "method": "user.getRecentTracks",
            "user":   username,
            "from":   from_time,
            "to":     now,
            "limit":  200,
            "page":   page,
        }, timeout=25)

        if "error" in data:
            raise LastfmError(str(data.get("message", data["error"])))

        rt          = data.get("recenttracks", {})
        raw         = rt.get("track", [])
        total_pages = int(rt.get("@attr", {}).get("totalPages", 1))

        for t in raw:
            if not t.get("date"):           # ignora "now playing"
                continue
            album_name  = (t.get("album")  or {}).get("#text", "").strip()
            artist_name = (t.get("artist") or {}).get("#text", "").strip()
            track_name  = t.get("name", "").strip()
            if not (album_name and artist_name and track_name):
                continue                    # scrobble sem álbum → descarta
            images = {img["size"]: img["#text"] for img in t.get("image", [])}
            scrobbles.append({
                "track":  track_name,
                "artist": artist_name,
                "album":  album_name,
                "cover":  _best_image(images),
            })

        print(f"      Página {page}/{total_pages}  ({len(scrobbles)} scrobbles até agora)…")
        if page >= total_pages:
            break
        page += 1

    if not scrobbles:
        print("  aviso: Nenhum scrobble encontrado no período.")
        return []
    print(f"  ok {len(scrobbles)} scrobbles carregados")

    # 2. Conta plays por faixa e coleta melhor cover por álbum
    # key: (norm_artist, norm_track, norm_album) → plays
    track_plays: defaultdict[tuple, int] = defaultdict(int)
    track_names: dict[tuple, tuple]      = {}   # norm_key → (artist, track)
    track_album: dict[tuple, str]        = {}   # norm_key → album_key
    album_cover: dict[str, str]          = {}   # album_key → cover_url

    for s in scrobbles:
        ak  = f"{normalize(s['artist'])}||{normalize(s['album'])}"
        tk  = (normalize(s["artist"]), normalize(s["track"]), normalize(s["album"]))
        track_plays[tk] += 1
        if tk not in track_names:
            track_names[tk] = (s["artist"], s["track"])
            track_album[tk] = ak
        if s["cover"] and ak not in album_cover:
            album_cover[ak] = s["cover"]

    # 3. Busca duração de faixas únicas em paralelo
    # dedup by (artist, track) since duration doesn't vary by album
    unique_keys = list(track_plays.keys())
    unique_artist_track = list({(tk[0], tk[1]): tk for tk in unique_keys}.values())
    durations: dict[tuple, int] = {}   # keyed by (norm_artist, norm_track, norm_album)

    # duration lookup key is (norm_artist, norm_track) — same for all albums
    dur_cache: dict[tuple, int] = {}   # (norm_artist, norm_track) → seconds
    if rank_by == "time":
        print(f"  Buscando duração de {len(unique_artist_track)} faixas únicas "
              f"({TRACK_INFO_WORKERS} workers)…")

    def fetch_dur(tk: tuple) -> tuple[tuple, int]:
        artist, track = track_names[tk]
        info = _fetch_track_info(api_key, artist, track)
        if not info:
            return tk, 0
        dur_raw = int(info.get("duration") or 0)
        # Last.fm retorna ms em track.getInfo; >3600 indica ms (>1h em segundos seria raro)
        dur_s = dur_raw // 1000 if dur_raw > 3600 else dur_raw
        return tk, dur_s

    with ThreadPoolExecutor(max_workers=TRACK_INFO_WORKERS) as pool:
        futures = {pool.submit(fetch_dur, k): k for k in (unique_artist_track if rank_by == "time" else [])}
        done = 0
        for f in as_completed(futures):
            done += 1
            try:
                tk, dur = f.result()
                dur_cache[(tk[0], tk[1])] = dur
            except Exception:
                pass
            if done % 25 == 0 or done == len(unique_artist_track):
                print(f"      {done}/{len(unique_artist_track)} faixas processadas…")

    # 4. Agrega por álbum: soma duration × plays
    album_time: defaultdict[str, int] = defaultdict(int)
    album_meta: dict[str, dict]       = {}

    for tk, plays in track_plays.items():
        dur = dur_cache.get((tk[0], tk[1])) or DEFAULT_TRACK_DURATION
        ak  = track_album[tk]
        album_time[ak] += dur * plays if rank_by == "time" else plays

        if ak not in album_meta:
            # recupera nome legível do álbum e artista do primeiro scrobble
            artist_n, _ = track_names[tk]
            # encontra nome original do álbum pesquisando nos scrobbles
            album_n = next(
                (s["album"] for s in scrobbles
                 if normalize(s["artist"]) == tk[0]
                 and f"{normalize(s['artist'])}||{normalize(s['album'])}" == ak),
                ak.split("||")[-1],
            )
            album_meta[ak] = {
                "name":      album_n,
                "artist":    artist_n,
                "cover_url": album_cover.get(ak, ""),
            }

    # 5. Ordena e retorna top N
    sorted_albums = sorted(album_time.items(), key=lambda x: x[1], reverse=True)

    # fallback de capa: álbuns sem imagem nos scrobbles → album.getInfo
    top_aks = [ak for ak, _ in sorted_albums[:limit]]
    missing = [ak for ak in top_aks if not album_meta.get(ak, {}).get("cover_url")]
    if missing:
        print(f"  Buscando capa via album.getInfo para {len(missing)} álbum(ns) sem imagem…")
        for ak in missing:
            meta = album_meta.get(ak)
            if not meta:
                continue
            url = _fetch_album_cover(api_key, meta["artist"], meta["name"])
            if url:
                album_meta[ak]["cover_url"] = url
                print(f"      ✓  {meta['artist']} — {meta['name']}")
            else:
                print(f"      —  sem capa: {meta['artist']} — {meta['name']}")

    result = []
    for ak, score in sorted_albums[:limit]:
        meta = album_meta[ak]
        entry = {
            "name":       meta["name"],
            "artist":     meta["artist"],
            "plays":      score,
            "cover_url":  meta["cover_url"],
            "time_label": None,
        }
        if rank_by == "time":
            hh, rem = divmod(score, 3600)
            mm, ss  = divmod(rem, 60)
            entry["time_label"] = f"{hh}h{mm:02d}m" if hh else f"{mm}m{ss:02d}s"
        result.append(entry)

    return result


# compatibilidade com o nome antigo
def get_top_albums_by_listening_time(api_key: str, username: str, limit: int = TOP_N) -> list[dict]:
    return get_top_albums_in_window(api_key, username, limit, rank_by="time")


def window_bounds() -> tuple[int, int]:
    """(from, to) em epoch: janela explícita (configure) ou os últimos PERIOD_DAYS até agora."""
    to   = WINDOW_TO or int(_time.time())
    from_ = WINDOW_FROM or to - PERIOD_DAYS * 24 * 3600
    return from_, to


# ── RYM CSV ──────────────────────────────────────────────────

def append_rym_rating(csv_path: str, artist: str, album: str, rating: str) -> None:
    """
    Adiciona uma nova entrada de rating ao CSV de export do RYM.

    Como não temos o RYM Album ID nem a data de lançamento, usamos valores
    vazios nesses campos — o que é suficiente para que load_rym_ratings
    encontre o match em execuções futuras.

    O artista vai inteiro no campo 'Last Name' (padrão do RYM para bandas).
    Se o CSV não existir ainda, cria com o header correto.
    """
    path = Path(csv_path)
    file_exists = path.exists() and path.stat().st_size > 0

    # lê o header existente para manter consistência de espaçamento
    if file_exists:
        with open(path, encoding="utf-8-sig", newline="") as f:
            header = next(csv.reader(f), [])
        fieldnames = [h.strip() for h in header]
    else:
        fieldnames = [
            "RYM Album", "First Name", "Last Name",
            "First Name localized", "Last Name localized",
            "Title", "Release_Date", "Rating",
            "Ownership", "Purchase Date", "Media Type",
        ]

    new_row = {h: "" for h in fieldnames}
    new_row["RYM Album"]  = "0"       # placeholder — sem ID oficial
    new_row["Last Name"]  = artist    # bandas e nomes completos ficam em Last Name
    new_row["Title"]      = album
    new_row["Rating"]     = rating
    new_row["Ownership"]  = "n"

    needs_newline = False
    if file_exists:
        with open(path, "rb") as f:
            f.seek(-1, 2)
            needs_newline = f.read(1) not in (b"\n", b"\r")

    with open(path, "a", encoding="utf-8", newline="") as f:
        if needs_newline:
            f.write("\r\n")
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore", quoting=csv.QUOTE_ALL)
        if not file_exists:
            writer.writeheader()
        writer.writerow(new_row)


def load_rym_ratings(csv_path: str) -> tuple[dict[tuple, str], list[tuple[str, str, str]]]:
    """
    Lê o CSV de export do RYM e retorna:
      - index: dict de (artist_variant, title_variant) → rating_str
                com múltiplas chaves por entrada para matching robusto
      - rows: lista de (artist_norm, title_norm, rating_str) para fallback
               de containment matching

    Estratégias aplicadas:
      • Stripping de espaços nos nomes das colunas (bug do CSV do RYM)
      • html.unescape para &amp; etc.
      • Variantes de artista: com/sem 'The', com/sem feat/collab
      • Variantes de título: com/sem sufixo de edição/remaster
    """
    index: dict[tuple, str] = {}
    rows:  list[tuple[str, str, str]] = []
    path = Path(csv_path)

    if not path.exists():
        print(f"  aviso: CSV do RYM não encontrado em '{csv_path}'. Ratings serão omitidos.")
        print("      Para exportar: no RYM → Settings → Export Data → Export Ratings")
        return index, rows

    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        # O CSV do RYM às vezes tem espaços nos nomes das colunas (' First Name')
        if reader.fieldnames:
            reader.fieldnames = [h.strip() for h in reader.fieldnames]

        for raw_row in reader:
            # strip também nas chaves de cada row (por segurança)
            row = {k.strip(): v for k, v in raw_row.items()}

            title      = _unescape(row.get("Title") or row.get("Album") or "")
            first      = _unescape(row.get("First Name") or "")
            last       = _unescape(row.get("Last Name") or "")
            first_loc  = _unescape(row.get("First Name localized") or "")
            last_loc   = _unescape(row.get("Last Name localized") or "")
            artist_raw = _unescape(row.get("Artist") or f"{first} {last}".strip())
            artist_loc = f"{first_loc} {last_loc}".strip()
            rating_raw = (row.get("Rating") or "").strip()

            if not (title and rating_raw and rating_raw not in ("0", "")):
                continue

            # candidatos de artista: nome principal + nome localizado (se houver)
            artist_candidates = [artist_raw]
            if artist_loc:
                artist_candidates.append(artist_loc)

            title_vars = _title_variants(title)

            for art in artist_candidates:
                art_vars = _artist_variants(art)
                # guarda a chave mais canônica para containment fallback
                if art_vars and title_vars:
                    rows.append((art_vars[0], title_vars[0], rating_raw))
                # indexa todas as combinações de variantes
                for av in art_vars:
                    for tv in title_vars:
                        key = (av, tv)
                        if key not in index:
                            index[key] = rating_raw

    return index, rows


def find_rating(
    index: dict[tuple, str],
    rows:  list[tuple[str, str, str]],
    artist: str,
    album:  str,
) -> str | None:
    """
    Busca o rating de um álbum usando múltiplas estratégias em ordem de confiança:

    1. Lookup direto no index (cobre: strip 'The', strip feat, strip remaster)
    2. Containment fallback: se o título bate exato e o artista do RYM está
       contido no do Last.fm ou vice-versa (cobre: 'Blunt' ↔ 'Dean Blunt')
    """
    # 1. lookup por variantes
    for av in _artist_variants(artist):
        for tv in _title_variants(album):
            if (av, tv) in index:
                return index[(av, tv)]

    # 2. containment fallback (título exato + artista parcial)
    title_norms = set(_title_variants(album))
    artist_norms = set(_artist_variants(artist))
    for (rym_art, rym_title, rating) in rows:
        if rym_title not in title_norms:
            continue
        for an in artist_norms:
            if an in rym_art or rym_art in an:
                return rating

    return None


# ── cover art ────────────────────────────────────────────────

def _fetch_url_bytes(url: str, retries: int = 2) -> bytes | None:
    """Faz GET em url e retorna bytes se HTTP 200, None caso contrário.

    Tenta algumas vezes: uma falha transitória aqui na imagem principal
    (Last.fm) fazia o download cair no fallback do MusicBrainz e trazer a
    capa errada. Reduzir essas falhas mantém a capa correta da fonte primária.
    """
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200 and resp.content:
                return resp.content
        except Exception:
            pass
    return None


_MB_HEADERS = {"User-Agent": "weekly-music-grid/1.0 (github.com/user/weekly-music-grid)"}


def _musicbrainz_cover_url(artist: str, album: str) -> str:
    """
    Busca URL de capa via MusicBrainz + Cover Art Archive (sem chave).
    Retorna string vazia se não encontrar.
    """
    try:
        # 1. encontra o release-group
        resp = requests.get(
            "https://musicbrainz.org/ws/2/release-group",
            params={"query": f'artist:"{artist}" release:"{album}"', "fmt": "json", "limit": 5},
            headers=_MB_HEADERS,
            timeout=10,
        )
        if resp.status_code != 200:
            return ""
        groups = resp.json().get("release-groups", [])
        if not groups:
            return ""

        # A busca do MusicBrainz ranqueia por relevância de texto e faz match
        # fuzzy, então pode devolver: (a) o MESMO título de OUTRO artista (ex.:
        # "Eu Não Sou Santo" do Bezerra da Silva antes do Link do Zap), ou (b)
        # OUTRO álbum do artista certo (ex.: "Kalledy" do Link do Zap). Só
        # aceitamos um release-group em que ARTISTA e TÍTULO batam com o pedido.
        want_artist = normalize(_strip_feat(artist))
        want_album  = normalize(album)
        matched = None
        for g in groups:
            credited = "".join(
                ac.get("name", "") if isinstance(ac, dict) else str(ac)
                for ac in g.get("artist-credit", [])
            )
            got_artist = normalize(credited)
            got_album  = normalize(g.get("title", ""))
            artist_ok = bool(want_artist and got_artist and
                             (want_artist in got_artist or got_artist in want_artist))
            album_ok  = bool(want_album and got_album and
                             (want_album in got_album or got_album in want_album))
            if artist_ok and album_ok:
                matched = g
                break
        if matched is None:
            return ""
        rg_id = matched["id"]

        # 2. busca a capa no Cover Art Archive
        ca_resp = requests.get(
            f"https://coverartarchive.org/release-group/{rg_id}",
            headers=_MB_HEADERS,
            timeout=10,
            allow_redirects=True,
        )
        if ca_resp.status_code != 200:
            return ""
        for img in ca_resp.json().get("images", []):
            if img.get("front"):
                return img.get("thumbnails", {}).get("large") or img.get("image", "")
    except Exception:
        pass
    return ""


def download_cover(url: str, size: int = COVER_SIZE,
                   artist: str = "", album: str = "") -> Image.Image:
    """
    Baixa a capa do álbum e retorna um PIL Image redimensionado.
    Tenta, em ordem:
      1. url fornecida (Last.fm)
      2. iTunes Search API  (se artist + album foram passados)
    """
    def _to_image(data: bytes) -> Image.Image | None:
        try:
            return Image.open(io.BytesIO(data)).convert("RGB")
        except Exception:
            return None

    # 1. URL do Last.fm
    if url:
        data = _fetch_url_bytes(url)
        if data:
            img = _to_image(data)
            if img:
                return img.resize((size, size), Image.LANCZOS)

    # 2. MusicBrainz Cover Art Archive fallback
    if artist and album:
        mb_url = _musicbrainz_cover_url(artist, album)
        if mb_url:
            data = _fetch_url_bytes(mb_url)
            if data:
                img = _to_image(data)
                if img:
                    return img.resize((size, size), Image.LANCZOS)

    # placeholder se tudo falhar
    img = Image.new("RGB", (size, size), (40, 40, 40))
    draw = ImageDraw.Draw(img)
    draw.text((size // 2, size // 2), "", fill=(80, 80, 80), anchor="mm")
    return img


# ── fonts ────────────────────────────────────────────────────

def _has_cjk(text: str) -> bool:
    """Retorna True se o texto contém caracteres CJK (japonês, chinês, coreano)."""
    for ch in text:
        cp = ord(ch)
        if (0x3000 <= cp <= 0x9FFF or 0xF900 <= cp <= 0xFAFF or
                0xFF00 <= cp <= 0xFFEF or 0x20000 <= cp <= 0x2FA1F):
            return True
    return False


def _font_renders_japanese(font: ImageFont.FreeTypeFont) -> bool:
    """
    Verifica se a fonte tem glyphs japoneses reais (não quadradinhos □).

    Estratégia: compara 'あ' (hiragana) com 'ア' (katakana).
    - Fonte com suporte JP: são caracteres diferentes → pixels diferentes ✓
    - Fonte sem suporte JP: ambos viram o mesmo □ → pixels idênticos ✗
    """
    try:
        size = 24
        def render(ch):
            img = Image.new("L", (size, size), 255)
            ImageDraw.Draw(img).text((0, 0), ch, fill=0, font=font)
            return bytes(img.tobytes())
        return render("あ") != render("ア")
    except Exception:
        return False


def _find_cjk_font_paths() -> list[str]:
    """
    Detecta automaticamente fontes CJK instaladas no sistema.
    Usa glob para encontrar os Hiragino mesmo com nomes em japonês.
    """
    candidates: list[str] = []

    # config manual tem prioridade absoluta
    if CJK_FONT_PATH:
        candidates.append(CJK_FONT_PATH)

    # macOS: Hiragino — a família principal de fontes japonesas da Apple.
    # Os arquivos têm nomes em japonês, então usamos glob para encontrá-los.
    for pattern in [
        "/System/Library/Fonts/*ゴシック*.ttc",           # ヒラギノ角ゴシック
        "/System/Library/Fonts/*ゴシック*.otf",
        "/System/Library/Fonts/Supplemental/*ゴシック*.ttc",
        "/System/Library/Fonts/Supplemental/*ゴシック*.otf",
        "/Library/Fonts/*ゴシック*.ttc",
        "/Library/Fonts/*ゴシック*.otf",
    ]:
        candidates.extend(sorted(glob.glob(pattern)))

    # macOS: outros com cobertura CJK ampla
    candidates += [
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/Supplemental/Hiragino Sans GB W3.ttc",
        "/Library/Fonts/Arial Unicode MS.ttf",
    ]

    # Homebrew / Linux: Noto CJK
    for pattern in [
        "/opt/homebrew/share/fonts/**/*Noto*CJK*.otf",
        "/opt/homebrew/share/fonts/**/*Noto*CJK*.ttc",
        "/usr/local/share/fonts/*Noto*CJK*.otf",
        "/Library/Fonts/Noto*CJK*.otf",
        "/Library/Fonts/Noto*CJK*.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKjp-Regular.otf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
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

    return candidates


def load_fonts() -> dict:
    """
    Carrega fontes do sistema com fallback automático.
    Prioriza fontes com suporte a CJK (japonês, chinês, coreano).
    Estrelas são desenhadas como polígonos vetoriais — sem necessidade de fonte.

    Se caracteres aparecerem como □, instale uma fonte CJK e configure CJK_FONT_PATH.
    macOS:  brew install --cask font-noto-sans-cjk-japanese
    """
    latin_candidates = [
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/SFNSText.ttf",
        "/Library/Fonts/Arial.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]
    cjk_candidates = _find_cjk_font_paths()

    def load_latin(size):
        for path in latin_candidates:
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
        return ImageFont.load_default()

    def load_cjk(size):
        """
        Tenta cada candidato CJK e só aceita se realmente renderiza japonês.
        Isso evita silenciosamente aceitar uma fonte que carrega mas não tem os glyphs.
        """
        # Fase 1: candidato que carrega E renderiza japonês
        for path in cjk_candidates:
            try:
                font = ImageFont.truetype(path, size)
                if _font_renders_japanese(font):
                    return font, path
            except Exception:
                continue
        # Fase 2: qualquer candidato que ao menos carregue (fallback sem garantia)
        for path in cjk_candidates:
            try:
                return ImageFont.truetype(path, size), path
            except Exception:
                continue
        return ImageFont.load_default(), None

    latin_sm = load_latin(13)
    latin_md = load_latin(15)

    cjk_sm, cjk_path = load_cjk(13)
    cjk_md, _        = load_cjk(15)

    if cjk_path and _font_renders_japanese(cjk_sm):
        print(f"  Fonte CJK: {cjk_path}")
    else:
        print("  aviso: Nenhuma fonte com suporte a japonês encontrada.")
        print("      Caracteres como ミドリ aparecerão como □.")
        print("      Solução rápida no macOS:")
        print("        brew install --cask font-noto-sans-cjk-japanese")
        print("      Depois adicione ao script:")
        print('        CJK_FONT_PATH = "/Library/Fonts/NotoSansCJKjp-Regular.otf"')
        print("      Ou rode: python find_cjk_font.py  para diagnóstico completo.")

    return {
        "latin_sm": latin_sm,
        "latin_md": latin_md,
        "cjk_sm":   cjk_sm,
        "cjk_md":   cjk_md,
    }


def pick_font(fonts: dict, text: str, size: str = "md") -> ImageFont.FreeTypeFont:
    """Escolhe latin ou CJK conforme o conteúdo do texto."""
    key = ("cjk" if _has_cjk(text) else "latin") + "_" + size
    return fonts.get(key, fonts["latin_md"])


# ── grid image ───────────────────────────────────────────────

def _star_points(cx: float, cy: float, r_out: float, r_in: float) -> list[tuple]:
    """Pontos de uma estrela de 5 pontas centralizada em (cx, cy)."""
    pts = []
    for i in range(10):
        angle = math.pi * i / 5 - math.pi / 2
        r = r_out if i % 2 == 0 else r_in
        pts.append((cx + r * math.cos(angle), cy + r * math.sin(angle)))
    return pts


def _overlay_box(cover: Image.Image, box_w: int, box_h: int,
                 from_top: int | None = None, from_bottom: int | None = None) -> tuple[int, int]:
    """Calcula (x, y) para centralizar um box horizontalmente na capa."""
    x = (cover.width - box_w) // 2
    if from_top is not None:
        y = from_top
    else:
        y = cover.height - box_h - from_bottom
    return x, y


def _draw_pill(layer: Image.Image, x: int, y: int, w: int, h: int):
    """Desenha um retângulo arredondado semi-transparente."""
    d = ImageDraw.Draw(layer)
    d.rounded_rectangle([x, y, x + w, y + h], radius=6, fill=(15, 15, 15, OVERLAY_ALPHA))


def overlay_stars(cover: Image.Image, stars_str: str):
    """
    Desenha estrelas como polígonos vetoriais sobre a capa (topo, centralizado).
    Estrelas cheias: polígono amarelo. Meia-estrela: polígono mascarado à esquerda.
    """
    n_full = stars_str.count("★")
    has_half = "½" in stars_str
    n_total = n_full + (1 if has_half else 0)
    if n_total == 0:
        return

    r_out = STAR_SIZE
    r_in  = int(r_out * 0.42)
    gap   = 5
    diam  = r_out * 2

    stars_w = n_total * diam + (n_total - 1) * gap
    box_w   = stars_w + OVERLAY_PAD_X * 2
    box_h   = diam + OVERLAY_PAD_Y * 2

    bx, by = _overlay_box(cover, box_w, box_h, from_top=OVERLAY_MARGIN)

    # fundo semi-transparente
    bg_layer = Image.new("RGBA", cover.size, (0, 0, 0, 0))
    _draw_pill(bg_layer, bx, by, box_w, box_h)
    cover.alpha_composite(bg_layer)

    # estrelas
    star_layer = Image.new("RGBA", cover.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(star_layer)
    cy_star = by + box_h // 2

    for i in range(n_full):
        cx = bx + OVERLAY_PAD_X + i * (diam + gap) + r_out
        pts = _star_points(cx, cy_star, r_out, r_in)
        sd.polygon(pts, fill=(*STAR_COLOR, 255))

    if has_half:
        cx = bx + OVERLAY_PAD_X + n_full * (diam + gap) + r_out
        # cria estrela completa em imagem temporária e mascara metade direita
        tmp_size = (r_out * 2 + 4, r_out * 2 + 4)
        tmp = Image.new("RGBA", tmp_size, (0, 0, 0, 0))
        td = ImageDraw.Draw(tmp)
        pts_local = _star_points(r_out + 2, r_out + 2, r_out, r_in)
        td.polygon(pts_local, fill=(*STAR_COLOR, 255))
        # apaga metade direita
        td.rectangle([r_out + 2, 0, tmp_size[0], tmp_size[1]], fill=(0, 0, 0, 0))
        paste_x = int(cx) - r_out - 2
        paste_y = int(cy_star) - r_out - 2
        star_layer.alpha_composite(tmp, dest=(max(paste_x, 0), max(paste_y, 0)))

    cover.alpha_composite(star_layer)


def overlay_label(cover: Image.Image, text: str, font: ImageFont.FreeTypeFont):
    """
    Desenha artista – álbum sobre a capa (rodapé, centralizado).
    O box se ajusta ao tamanho do texto.
    """
    bbox   = font.getbbox(text)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    box_w  = text_w + OVERLAY_PAD_X * 2
    box_h  = text_h + OVERLAY_PAD_Y * 2

    bx, by = _overlay_box(cover, box_w, box_h, from_bottom=OVERLAY_MARGIN)

    bg_layer = Image.new("RGBA", cover.size, (0, 0, 0, 0))
    _draw_pill(bg_layer, bx, by, box_w, box_h)
    cover.alpha_composite(bg_layer)

    txt_layer = Image.new("RGBA", cover.size, (0, 0, 0, 0))
    td = ImageDraw.Draw(txt_layer)
    td.text(
        (bx + OVERLAY_PAD_X - bbox[0], by + OVERLAY_PAD_Y - bbox[1]),
        text,
        fill=(255, 255, 255, 255),
        font=font,
    )
    cover.alpha_composite(txt_layer)


def build_grid(
    albums:          list[dict],
    index:           dict[tuple, str],
    rows_fb:         list[tuple[str, str, str]],
    manual_ratings:  dict[int, str] | None = None,
    mode:            str = "scrobbles",
    show_time_label: bool = SHOW_TIME_LABEL,
) -> Image.Image:
    """
    Monta a imagem final. Capas sem gaps — informações sobrepostas via overlays.
    manual_ratings:  dict de {índice_do_álbum: rating_str} para ratings inseridos
                     manualmente pelo usuário, que têm prioridade sobre o auto-match.
    mode:            "scrobbles" | "listening_time" — afeta o sub-label exibido na capa.
    show_time_label: se True, prefixo de tempo é exibido no label inferior (listening_time only).
    """
    fonts    = load_fonts()
    cols     = COLUMNS
    rows     = math.ceil(len(albums) / cols)
    canvas_w = cols * COVER_SIZE
    canvas_h = rows * COVER_SIZE
    manual_ratings = manual_ratings or {}

    canvas = Image.new("RGB", (canvas_w, canvas_h), BG_COLOR)

    for i, album in enumerate(albums):
        col = i % cols
        row = i // cols
        ox  = col * COVER_SIZE
        oy  = row * COVER_SIZE

        print(f"  Baixando capa: {album['artist']} — {album['name']}")
        cover = download_cover(album["cover_url"],
                               artist=album["artist"],
                               album=album["name"]).convert("RGBA")

        # ── overlay inferior: Artista – Álbum  [+ tempo se ativado] ──
        base_label = truncate(f"{album['artist']} – {album['name']}", 32)
        if mode == "listening_time" and show_time_label and album.get("time_label"):
            label = f"{album['time_label']}  {base_label}"
        else:
            label = base_label
        font = pick_font(fonts, label, "sm")
        overlay_label(cover, label, font)

        # ── overlay superior: estrelas ────────────────────────
        # manual_ratings tem prioridade; fallback para auto-match
        rating = manual_ratings.get(i) or find_rating(index, rows_fb, album["artist"], album["name"])
        stars  = rating_to_stars(rating) if rating else None
        if stars:
            overlay_stars(cover, stars)

        canvas.paste(cover.convert("RGB"), (ox, oy))

    return canvas


# ── main ─────────────────────────────────────────────────────

def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Gera uma grade PNG dos álbuns mais ouvidos com ratings do RYM.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--username", default=LASTFM_USERNAME, help="username do Last.fm")
    p.add_argument("--csv", default=RYM_CSV_PATH, help="caminho do CSV de export do RYM")
    p.add_argument("--output", default=OUTPUT_PATH, help="prefixo do arquivo de saída")
    p.add_argument("--period", choices=list(_PERIOD_PRESETS), default=PERIOD,
                   help="janela de análise (week=5×5/7d, month=8×8/30d)")
    p.add_argument("--since", metavar="'YYYY-MM-DD[ HH:MM]'",
                   help="início da janela (inclusive, hora local; sem hora = 00:00); só no modo listening_time")
    p.add_argument("--until", metavar="'YYYY-MM-DD[ HH:MM]'",
                   help="fim da janela (hora local; sem hora = dia inteiro); só no modo listening_time")
    p.add_argument("--mode", choices=["scrobbles", "listening_time"], default=MODE,
                   help="modo de ordenação")
    p.add_argument("--non-interactive", action="store_true",
                   help="não pede notas manualmente; álbuns sem rating ficam sem estrelas")
    p.add_argument("--list-unmatched", action="store_true",
                   help="apenas imprime (JSON) os álbuns sem rating e sai (não gera imagem)")
    p.add_argument("--add-rating", nargs=3, metavar=("ARTIST", "ALBUM", "RATING"),
                   help="adiciona uma nota (escala 1–10) ao CSV do RYM e sai")
    return p


def _parse_window_bound(text: str) -> datetime.datetime:
    """'YYYY-MM-DD' ou 'YYYY-MM-DD HH:MM' (hora local)."""
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(text, fmt)
        except ValueError:
            pass
    raise SystemExit(f"Data inválida: {text!r}. Use YYYY-MM-DD ou 'YYYY-MM-DD HH:MM'.")


# ── API programática ─────────────────────────────────────────
# Usada pela CLI (semaninha_cli.py). main() abaixo é só um wrapper fino.

@dataclass
class GridResult:
    username:  str
    csv_path:  str
    period:    str
    mode:      str
    albums:    list[dict]
    ratings:   dict[int, str]             # índice do álbum → rating (1–10)
    paths:     dict[str, str] = field(default_factory=dict)   # {"timed": …, "plain": …}

    @property
    def total(self) -> int:
        return len(self.albums)

    @property
    def matched(self) -> int:
        return len(self.ratings)

    @property
    def unmatched(self) -> list[dict]:
        return [
            {"artist": a["artist"], "album": a["name"]}
            for i, a in enumerate(self.albums) if i not in self.ratings
        ]


def configure(
    period: str = "week",
    since: str | None = None,
    until: str | None = None,
    api_key: str | None = None,
    cjk_font: str | None = None,
    ignored_artists: set[str] | None = None,
    ignored_albums: set[str] | None = None,
    ignored_pairs: set[tuple[str, str]] | None = None,
    top_n: int | None = None,
    columns: int | None = None,
    window_from: int | None = None,
    window_to: int | None = None,
) -> None:
    """
    Ajusta os globais do módulo. Chame antes de fetch_albums()/generate().
    period define o preset (grid e janela padrão); top_n/columns sobrescrevem o grid;
    since/until (strings) ou window_from/window_to (epoch, fim exclusivo) sobrescrevem a janela.
    """
    global TOP_N, COLUMNS, PERIOD_DAYS, LASTFM_PERIOD, WINDOW_FROM, WINDOW_TO
    global LASTFM_API_KEY, CJK_FONT_PATH
    global IGNORED_ARTISTS, IGNORED_ALBUMS, IGNORED_ARTIST_ALBUMS

    if period not in _PERIOD_PRESETS:
        raise ValueError(f"período inválido: {period!r} (use {', '.join(_PERIOD_PRESETS)})")
    preset = _PERIOD_PRESETS[period]
    TOP_N, COLUMNS             = preset["top_n"], preset["columns"]
    PERIOD_DAYS, LASTFM_PERIOD = preset["days"], preset["lastfm"]

    WINDOW_FROM = int(_parse_window_bound(since).timestamp()) if since else None
    if until:
        bound = _parse_window_bound(until)
        if len(until) == 10:                # só data → inclui o dia inteiro
            bound += datetime.timedelta(days=1)
        WINDOW_TO = int(bound.timestamp()) - 1
    else:
        WINDOW_TO = None
    if window_from is not None:
        WINDOW_FROM = int(window_from)
    if window_to is not None:
        WINDOW_TO = int(window_to) - 1

    if top_n:
        TOP_N   = int(top_n)
        COLUMNS = int(columns) if columns else math.ceil(math.sqrt(TOP_N))
    elif columns:
        COLUMNS = int(columns)

    if api_key:
        LASTFM_API_KEY = api_key
    if cjk_font is not None:
        CJK_FONT_PATH = cjk_font
    if ignored_artists is not None:
        IGNORED_ARTISTS = set(ignored_artists)
    if ignored_albums is not None:
        IGNORED_ALBUMS = set(ignored_albums)
    if ignored_pairs is not None:
        IGNORED_ARTIST_ALBUMS = {tuple(p) for p in ignored_pairs}


def fetch_albums(username: str, mode: str = MODE) -> list[dict]:
    """Busca os top álbuns no Last.fm, aplica os filtros de ignorados e corta em TOP_N."""
    if LASTFM_API_KEY in ("", "SUA_API_KEY_AQUI"):
        raise LastfmError("LASTFM_API_KEY não configurada (crie em https://www.last.fm/api/account/create)")

    if mode == "scrobbles" and WINDOW_FROM is None and WINDOW_TO is None:
        print(f"  Modo: scrobbles  (top {TOP_N} por número de plays, período {LASTFM_PERIOD} do Last.fm)")
        albums = get_weekly_top_albums(LASTFM_API_KEY, username, limit=_fetch_limit())
    elif mode == "scrobbles":
        print(f"  Modo: scrobbles  (top {TOP_N} por número de plays na janela)")
        albums = get_top_albums_in_window(LASTFM_API_KEY, username, limit=_fetch_limit(), rank_by="plays")
    else:
        print(f"  Modo: listening_time  (top {TOP_N} por tempo total de escuta)")
        albums = get_top_albums_in_window(LASTFM_API_KEY, username, limit=_fetch_limit(), rank_by="time")

    for label, keep in (
        ("IGNORED_ARTISTS",       lambda a: a["artist"] not in IGNORED_ARTISTS),
        ("IGNORED_ALBUMS",        lambda a: a["name"] not in IGNORED_ALBUMS),
        ("IGNORED_ARTIST_ALBUMS", lambda a: (a["artist"], a["name"]) not in IGNORED_ARTIST_ALBUMS),
    ):
        before = len(albums)
        albums = [a for a in albums if keep(a)]
        if before - len(albums):
            print(f"  {before - len(albums)} álbum(ns) ocultado(s) por {label}")

    albums = albums[:TOP_N]
    if albums:
        print(f"  ok  {len(albums)} álbuns encontrados:")
        for a in albums:
            lead = f"{a['plays']:>4}x" if mode == "scrobbles" else f"{a.get('time_label', '?'):>8}"
            print(f"      {lead}  {a['artist']} — {a['name']}")
    return albums


def match_ratings(albums: list[dict], csv_path: str) -> tuple[dict, list, dict[int, str]]:
    """Casa cada álbum com o CSV do RYM. Devolve (index, rows_fb, ratings)."""
    print()
    if not Path(csv_path).is_file():
        print(f"  CSV de ratings ainda não existe ({csv_path}); seguindo sem notas.")
        return {}, [], {}
    print(f"  Carregando ratings do RYM de '{csv_path}'…")
    index, rows_fb = load_rym_ratings(csv_path)
    print(f"  ok  {len(rows_fb)} álbuns carregados do CSV")
    print()
    print("  Matching Last.fm ↔ RYM:")
    ratings: dict[int, str] = {}
    for i, a in enumerate(albums):
        rating = find_rating(index, rows_fb, a["artist"], a["name"])
        if rating:
            ratings[i] = rating
            print(f"      ✓  {rating_to_stars(rating) or '':<10} {a['artist']} — {a['name']}")
        else:
            print(f"      —  {'sem rating':<10} {a['artist']} — {a['name']}")
    print(f"\n  ok  {len(ratings)}/{len(albums)} álbuns com rating encontrado")
    return index, rows_fb, ratings


def prompt_ratings(albums: list[dict], ratings: dict[int, str], csv_path: str) -> None:
    """Pergunta no terminal a nota dos álbuns sem rating e salva no CSV (altera `ratings`)."""
    pending = [i for i in range(len(albums)) if i not in ratings]
    if not pending:
        return
    print()
    print("  Álbuns sem rating — digite uma nota de 1–10 ou Enter para deixar sem:")
    print("       (escala RYM: 10=★★★★★  9=★★★★½  8=★★★★  7=★★★½ ...)")
    print()
    for i in pending:
        a = albums[i]
        label = f"{a['artist']} — {a['name']}"
        while True:
            raw = input(f"  [{label}]  nota: ").strip()
            if raw == "":
                break
            try:
                val = float(raw)
            except ValueError:
                print("         aviso: Entrada inválida. Digite um número (ex: 8) ou Enter.")
                continue
            if 1 <= val <= 10:
                ratings[i] = raw
                print(f"         → {rating_to_stars(raw)}")
                append_rym_rating(csv_path, a["artist"], a["name"], raw)
                print(f"         Salvo em '{csv_path}'")
                break
            print("         aviso: Digite um número entre 1 e 10.")


def render(albums: list[dict], index: dict, rows_fb: list, ratings: dict[int, str],
           output: str, mode: str = MODE) -> dict[str, str]:
    """Gera a(s) imagem(ns). `output` é um prefixo (pode incluir pasta); devolve {rótulo: caminho}."""
    ts   = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    base = f"{output}_{ts}"
    Path(base).parent.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}
    print()
    if mode == "listening_time":
        for label, show_time in (("timed", True), ("plain", False)):
            path = f"{base}_timed.png" if show_time else f"{base}.png"
            print(f"  Montando grid ({'com' if show_time else 'sem'} tempo)…")
            img = build_grid(albums, index, rows_fb, manual_ratings=ratings,
                             mode=mode, show_time_label=show_time)
            img.save(path)
            print(f"  ok  Salvo: {path}")
            paths[label] = str(Path(path).resolve())
    else:
        path = f"{base}.png"
        print("  Montando grid…")
        build_grid(albums, index, rows_fb, manual_ratings=ratings, mode=mode).save(path)
        print(f"  ok  Salvo: {path}")
        paths["plain"] = str(Path(path).resolve())
    return paths


def generate(username: str, csv_path: str, output: str, mode: str = MODE,
             interactive: bool = False, dry_run: bool = False) -> GridResult:
    """
    Pipeline completo para uma conta: Last.fm → matching RYM → (prompt) → PNG(s).
    Com dry_run=True para antes de renderizar (útil para listar os sem rating).
    Levanta LastfmError se a API falhar; devolve albums=[] se não houver scrobbles.
    """
    print()
    print("Weekly Music Grid")
    print("─" * 44)
    w_from, w_to = window_bounds()
    print(f"  @{username}  |  grid {COLUMNS}×{math.ceil(TOP_N / COLUMNS)} (top {TOP_N})  |  CSV: {csv_path}")
    print(f"  {datetime.datetime.fromtimestamp(w_from):%d/%m/%Y %H:%M} → "
          f"{datetime.datetime.fromtimestamp(w_to + 1):%d/%m/%Y %H:%M}")

    albums = fetch_albums(username, mode)
    period = next((k for k, v in _PERIOD_PRESETS.items() if v["days"] == PERIOD_DAYS), "week")
    if not albums:
        print("  aviso: Nenhum álbum encontrado no período.")
        return GridResult(username, csv_path, period, mode, [], {})

    index, rows_fb, ratings = match_ratings(albums, csv_path)
    result = GridResult(username, csv_path, period, mode, albums, ratings)
    if dry_run:
        return result

    if interactive and sys.stdin.isatty():
        prompt_ratings(albums, ratings, csv_path)
    elif result.unmatched:
        print(f"  {len(result.unmatched)} álbum(ns) sem rating (sem estrelas)")

    result.paths = render(albums, index, rows_fb, ratings, output, mode)
    print()
    return result


def main():
    """Entrada antiga (flags planas). Prefira a CLI `semaninha`."""
    args = _build_arg_parser().parse_args()
    configure(period=args.period, since=args.since, until=args.until)

    if args.add_rating:
        artist, album, rating = args.add_rating
        append_rym_rating(args.csv, artist, album, rating)
        print(f"Salvo: {artist} — {album} → {rating}  em '{args.csv}'")
        return

    try:
        result = generate(args.username, args.csv, args.output, mode=args.mode,
                          interactive=not args.non_interactive and not args.list_unmatched,
                          dry_run=args.list_unmatched)
    except LastfmError as e:
        print(f"  erro: Erro Last.fm: {e}")
        sys.exit(1)

    if not result.albums:
        sys.exit(1)

    if args.list_unmatched:
        # Formato pensado para ser consumido por um agente/rotina, que então
        # busca a nota (ex: no RateYourMusic) e re-executa com --add-rating.
        payload = {
            "username": result.username, "csv": result.csv_path, "period": args.period,
            "total": result.total, "matched": result.matched, "unmatched": result.unmatched,
        }
        print("===UNMATCHED_JSON_START===")
        print(json.dumps(payload, ensure_ascii=False))
        print("===UNMATCHED_JSON_END===")


if __name__ == "__main__":
    main()
