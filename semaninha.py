#!/usr/bin/env python3
"""
semaninha — CLI interativa pros grids de álbuns (Last.fm + RateYourMusic).

    semaninha                 abre o menu interativo (setas + Enter)
    semaninha run [PERFIL…]   modo não-interativo (automação); veja `semaninha --help`

Estado (perfis, chave da API, pasta de saída) fica em ~/.config/semaninha/config.json
(ou no arquivo apontado por $SEMANINHA_CONFIG).
"""

from __future__ import annotations

import argparse
import calendar
import contextlib
import datetime as dt
import io
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

import questionary
import requests
from questionary import Choice, Style

import weekly_music_grid as engine

PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("SEMANINHA_CONFIG")
                   or Path.home() / ".config" / "semaninha" / "config.json").expanduser()
PRESET_HOUR = 11          # presets começam/terminam às 11:00
FRIDAY      = 4           # datetime.weekday()
DT_FMT      = "%Y-%m-%d %H:%M"
WEEKDAYS_PT = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]

STYLE = Style([
    ("qmark",       "fg:#ffc332 bold"),
    ("question",    "bold"),
    ("answer",      "fg:#ffc332"),
    ("pointer",     "fg:#ffc332 bold"),
    ("highlighted", "fg:#ffc332 bold"),
    ("selected",    "fg:#ffc332"),
    ("instruction", "fg:#888888"),
    ("text",        ""),
])


# ── estado persistente ───────────────────────────────────────

def default_output_dir() -> Path:
    pictures = Path.home() / "Pictures"
    return (pictures if pictures.is_dir() else Path.home()) / "Semaninha"


DEFAULT_STORE: dict = {
    "lastfm_api_key": "",
    "output_dir": str(default_output_dir()),
    "cjk_font": "",
    "ignore": {"artists": [], "albums": [], "pairs": []},
    "profiles": [],
    "last_profile": None,
    "last_custom": None,
}


@dataclass
class Profile:
    name:   str
    lastfm: str
    rym:    str
    csv:    str

    @property
    def csv_path(self) -> Path:
        return Path(self.csv).expanduser()

    @property
    def slug(self) -> str:
        return "".join(c for c in self.lastfm.lower() if c.isalnum() or c in "-_") or self.name

    def label(self) -> str:
        return f"{self.name}  (@{self.lastfm} · RYM {self.rym} · {self.csv_path.name})"


def load_store() -> dict:
    store = json.loads(json.dumps(DEFAULT_STORE))
    if CONFIG_PATH.is_file():
        try:
            store.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError as e:
            fail(f"{CONFIG_PATH}: JSON inválido: {e}")
    store["ignore"] = {**DEFAULT_STORE["ignore"], **(store.get("ignore") or {})}
    return store


def save_store(store: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(store, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def profiles(store: dict) -> list[Profile]:
    return [Profile(**{k: p.get(k, "") for k in ("name", "lastfm", "rym", "csv")}) for p in store["profiles"]]


def find_profile(store: dict, name: str) -> Profile | None:
    n = name.lower()
    for p in profiles(store):
        if n in (p.name.lower(), p.lastfm.lower(), p.rym.lower()):
            return p
    return None


def upsert_profile(store: dict, prof: Profile, old_name: str | None = None) -> None:
    lst = store["profiles"]
    key = (old_name or prof.name).lower()
    for i, p in enumerate(lst):
        if p["name"].lower() == key:
            lst[i] = asdict(prof)
            break
    else:
        lst.append(asdict(prof))


def api_key(store: dict) -> str:
    return os.environ.get("LASTFM_API_KEY") or store.get("lastfm_api_key") or ""


# ── consultas (presets + personalizada) ──────────────────────

@dataclass
class Query:
    kind:    str              # "week" | "month" | "custom"
    start:   dt.datetime      # inclusive
    end:     dt.datetime      # exclusivo
    rows:    int              # grade = rows × columns álbuns
    columns: int
    mode:    str = "listening_time"

    @property
    def top_n(self) -> int:
        return self.rows * self.columns

    @property
    def grid_label(self) -> str:
        return f"{self.rows}×{self.columns} ({self.top_n} álbuns)"

    @property
    def file_prefix(self) -> str:
        return {"week": "weekly", "month": "monthly"}.get(self.kind, "custom") + "_grid"

    def describe(self) -> str:
        return (f"{fmt_dt(self.start)} → {fmt_dt(self.end)}  ·  "
                f"{self.grid_label}  ·  "
                f"{'tempo de escuta' if self.mode == 'listening_time' else 'scrobbles'}")


def fmt_dt(d: dt.datetime) -> str:
    return f"{WEEKDAYS_PT[d.weekday()]} {d:%d/%m/%Y %H:%M}"


def preset_week(now: dt.datetime | None = None) -> Query:
    """11h da penúltima sexta → 11h da última sexta (hoje conta, se for sexta)."""
    now = now or dt.datetime.now()
    last_friday = now.date() - dt.timedelta(days=(now.weekday() - FRIDAY) % 7)
    end = dt.datetime.combine(last_friday, dt.time(PRESET_HOUR))
    return Query("week", end - dt.timedelta(days=7), end, rows=5, columns=5)


def preset_month(now: dt.datetime | None = None) -> Query:
    """11h do dia 1 do mês passado → 11h do dia 1 deste mês."""
    now = now or dt.datetime.now()
    first_this = now.date().replace(day=1)
    first_prev = (first_this - dt.timedelta(days=1)).replace(day=1)
    return Query("month",
                 dt.datetime.combine(first_prev, dt.time(PRESET_HOUR)),
                 dt.datetime.combine(first_this, dt.time(PRESET_HOUR)),
                 rows=8, columns=8)


def parse_dt(text: str) -> dt.datetime | None:
    t = text.strip()
    for fmt in (DT_FMT, "%Y-%m-%d %H", "%Y-%m-%d", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(t, fmt)
        except ValueError:
            continue
    return None


def parse_grid(text: str) -> tuple[int, int]:
    """'4x6' / '4×6' / '4:6' → (4 linhas, 6 colunas)."""
    m = re.fullmatch(r"\s*(\d+)\s*[x×X:]\s*(\d+)\s*", text or "")
    if not m or int(m[1]) < 1 or int(m[2]) < 1:
        fail(f"grade inválida: {text!r} (use LINHASxCOLUNAS, ex.: 4x6)")
    return int(m[1]), int(m[2])


def custom_query(start: str, end: str, rows: int, columns: int, mode: str) -> Query:
    s, e = parse_dt(start), parse_dt(end)
    if s is None or e is None:
        fail(f"data inválida ({start!r} / {end!r}); use YYYY-MM-DD ou 'YYYY-MM-DD HH:MM'")
    if len(end.strip()) == 10:              # só a data → inclui o dia inteiro
        e += dt.timedelta(days=1)
    if e <= s:
        fail("o fim da janela precisa ser depois do início")
    if rows < 1 or columns < 1:
        fail("linhas e colunas precisam ser ≥ 1")
    return Query("custom", s, e, rows, columns, mode)


# ── execução ─────────────────────────────────────────────────

@dataclass
class RunResult:
    profile:   Profile
    query:     Query
    albums:    list[dict]
    ratings:   dict[int, str]
    paths:     dict[str, str]
    error:     str | None = None

    @property
    def unmatched(self) -> list[dict]:
        return [{"artist": a["artist"], "album": a["name"]}
                for i, a in enumerate(self.albums) if i not in self.ratings]

    def to_json(self) -> dict:
        d = {"profile": self.profile.name, "lastfm": self.profile.lastfm, "rym": self.profile.rym,
             "csv": str(self.profile.csv_path),
             "window": {"start": self.query.start.strftime(DT_FMT), "end": self.query.end.strftime(DT_FMT)},
             "grid": {"rows": self.query.rows, "columns": self.query.columns, "albums": self.query.top_n},
             "mode": self.query.mode}
        if self.error:
            d["error"] = self.error
            return d
        d.update({
            "total": len(self.albums), "matched": len(self.ratings),
            "unmatched": self.unmatched, "paths": self.paths,
            "albums": [{"artist": a["artist"], "album": a["name"], "rating": self.ratings.get(i),
                        "time": a.get("time_label"), "plays": a.get("plays")}
                       for i, a in enumerate(self.albums)],
        })
        return d


def configure_engine(store: dict, q: Query) -> None:
    ig = store["ignore"]
    engine.configure(
        period="month" if q.kind == "month" else "week",
        api_key=api_key(store),
        cjk_font=store.get("cjk_font") or None,
        ignored_artists=set(ig.get("artists", [])),
        ignored_albums=set(ig.get("albums", [])),
        ignored_pairs={tuple(p) for p in ig.get("pairs", [])},
        top_n=q.top_n, columns=q.columns,
        window_from=int(q.start.timestamp()), window_to=int(q.end.timestamp()),
    )


@contextlib.contextmanager
def engine_logs(quiet: bool = False, to_stderr: bool = False):
    if quiet:
        with contextlib.redirect_stdout(io.StringIO()):
            yield
    elif to_stderr:
        with contextlib.redirect_stdout(sys.stderr):
            yield
    else:
        yield


def run_query(store: dict, prof: Profile, q: Query, *, quiet: bool = False, to_stderr: bool = False,
              ask_ratings=None, dry_run: bool = False) -> RunResult:
    """
    Pipeline de uma consulta. ask_ratings(albums, ratings, csv_path) é um hook opcional,
    chamado antes de renderizar, para completar notas (o modo interativo usa o questionary).
    """
    if not api_key(store):
        return RunResult(prof, q, [], {}, {}, error="chave da API do Last.fm não configurada")
    configure_engine(store, q)
    out_prefix = str(Path(store["output_dir"]).expanduser() / f"{q.file_prefix}_{prof.slug}")
    try:
        with engine_logs(quiet, to_stderr):
            print(f"\n🎵  semaninha · {prof.name}")
            print("─" * 44)
            print(f"  📅  {q.describe()}")
            albums = engine.fetch_albums(prof.lastfm, q.mode)
            if not albums:
                return RunResult(prof, q, [], {}, {})
            index, rows_fb, ratings = engine.match_ratings(albums, str(prof.csv_path))
        if dry_run:
            return RunResult(prof, q, albums, ratings, {})
        if ask_ratings:
            ask_ratings(albums, ratings, str(prof.csv_path))
        with engine_logs(quiet, to_stderr):
            paths = engine.render(albums, index, rows_fb, ratings, out_prefix, q.mode)
        return RunResult(prof, q, albums, ratings, paths)
    except engine.LastfmError as e:
        return RunResult(prof, q, [], {}, {}, error=f"Last.fm: {e}")
    except requests.RequestException as e:
        return RunResult(prof, q, [], {}, {}, error=f"rede: {e}")


def parse_rating(text: str) -> str | None:
    """'9' → '9'; estrelas: '4.5*', '4.5★', '4,5*' → '9'. None se inválido."""
    t = text.strip().replace(",", ".")
    stars = t.endswith(("*", "★"))
    t = t.rstrip("*★").strip()
    try:
        val = float(t)
    except ValueError:
        return None
    if stars:
        val *= 2
    if not 1 <= val <= 10 or (val * 2) % 1:
        return None
    return str(int(val)) if val.is_integer() else str(val)


def latest_paths(store: dict, prof: Profile, kind: str | None = None) -> dict[str, Path]:
    """Último par {timed, plain} gerado para o perfil (timestamp no nome do arquivo)."""
    out_dir = Path(store["output_dir"]).expanduser()
    kinds = [kind] if kind else ["weekly", "monthly", "custom"]
    best: tuple[str, str, list[Path]] | None = None   # (ts, stem, files)
    for k in kinds:
        stem = f"{k}_grid_{prof.slug}_"
        files = list(out_dir.glob(f"{stem}*.png"))
        for f in files:
            ts = f.stem[len(stem):].replace("_timed", "")
            if best is None or ts > best[0]:
                best = (ts, stem, files)
    if not best:
        return {}
    ts, stem, files = best
    return {("timed" if f.stem.endswith("_timed") else "plain"): f
            for f in files if f.stem.startswith(stem + ts)}


def open_files(paths: list[Path]) -> None:
    if not paths:
        return
    opener = "open" if platform.system() == "Darwin" else "xdg-open"
    if shutil.which(opener) is None:
        print(f"  ⚠️  '{opener}' não encontrado; abra manualmente.", file=sys.stderr)
        return
    subprocess.run([opener, *map(str, paths)], check=False)


def print_summary(r: RunResult) -> None:
    title = f"{r.profile.name} · {r.query.describe()}"
    print(f"\n━━ {title}")
    if r.error:
        print(f"  ❌ {r.error}")
        return
    if not r.albums:
        print("  (sem álbuns no período)")
        return
    print(f"  {len(r.ratings)}/{len(r.albums)} com rating")
    for label in ("timed", "plain"):
        if label in r.paths:
            print(f"  {label:<6} {r.paths[label]}")
    if r.unmatched:
        print("  sem rating:")
        for u in r.unmatched:
            print(f"    – {u['artist']} — {u['album']}")
    else:
        print("  sem rating: nenhum ✨")


def fail(msg: str, code: int = 2) -> None:
    print(f"❌  {msg}", file=sys.stderr)
    raise SystemExit(code)


# ── modo interativo ──────────────────────────────────────────

def ask(prompt):
    """Executa um prompt do questionary; Ctrl-C/Esc devolve None (= voltar)."""
    try:
        return prompt.ask()
    except KeyboardInterrupt:
        return None


BACK = "__back__"   # valor da opção "← Voltar" (questionary troca value=None pelo título)


def select(message: str, choices: list, default=None):
    r = ask(questionary.select(message, choices=choices, default=default, style=STYLE,
                               instruction="(↑↓ move · Enter escolhe · Ctrl-C volta)"))
    return None if r == BACK else r


def text(message: str, default: str = "", validate=None, **kw):
    return ask(questionary.text(message, default=default, validate=validate, style=STYLE, **kw))


def confirm(message: str, default: bool = True) -> bool:
    return bool(ask(questionary.confirm(message, default=default, style=STYLE)))


def banner(store: dict) -> None:
    n = len(store["profiles"])
    print("\n🎵  semaninha  ·  grids do Last.fm com notas do RYM")
    print(f"    {n} perfil(is) · saída em {store['output_dir']}\n")


def tui_pick_profile(store: dict) -> Profile | None:
    while True:
        profs = profiles(store)
        choices = [Choice(p.label(), p) for p in profs]
        choices.append(Choice("＋ Novo perfil…", "new"))
        choices.append(Choice("← Voltar", BACK))
        default = next((c for c in choices if isinstance(c.value, Profile)
                        and c.value.name == store.get("last_profile")), None)
        chosen = select("Qual perfil?", choices, default=default)
        if chosen is None:
            return None
        if chosen == "new":
            prof = tui_edit_profile(store)
            if prof is None:
                continue
            return prof
        return chosen


def _validate_nonempty(v: str):
    return True if v.strip() else "não pode ficar vazio"


def tui_edit_profile(store: dict, current: Profile | None = None) -> Profile | None:
    print("\n  Perfil" + (f" · editando {current.name}" if current else " novo"))
    name = text("Nome do perfil (apelido):", default=current.name if current else "", validate=_validate_nonempty)
    if name is None:
        return None
    lastfm = text("Usuário do Last.fm:", default=current.lastfm if current else "", validate=_validate_nonempty)
    if lastfm is None:
        return None
    rym = text("Chave/usuário do RateYourMusic:", default=current.rym if current else lastfm,
               validate=_validate_nonempty)
    if rym is None:
        return None
    downloads = Path.home() / "Downloads"
    csv_default = current.csv if current else (str(downloads) + "/" if downloads.is_dir() else "")
    print("  O CSV é o export do RateYourMusic (Settings → Export Data → Export ratings).")
    print("  Pode deixar vazio: as notas que você der na CLI ficam num CSV próprio do perfil.")
    csv = ask(questionary.path("CSV com os ratings do RYM (Tab completa; vazio = nenhum):",
                               default=csv_default, style=STYLE))
    if csv is None:
        return None
    csv = csv.strip()
    slug = "".join(c for c in name.strip().lower() if c.isalnum() or c in "-_") or "perfil"
    if not csv or Path(csv).expanduser().is_dir():
        csv_path = CONFIG_PATH.parent / f"ratings_{slug}.csv"
        print(f"  → as notas deste perfil vão para {csv_path}")
    else:
        csv_path = Path(csv).expanduser()
        if not csv_path.is_file():
            print(f"  ⚠️  {csv_path} não existe ainda; ele é criado na primeira nota.")
            if not confirm("Salvar assim mesmo?", default=True):
                return None
    prof = Profile(name.strip(), lastfm.strip(), rym.strip(), str(csv_path.resolve()))
    upsert_profile(store, prof, old_name=current.name if current else None)
    store["last_profile"] = prof.name
    save_store(store)
    print(f"  ✅ perfil '{prof.name}' salvo")
    return prof


def tui_profiles(store: dict) -> None:
    while True:
        profs = profiles(store)
        choices = [Choice(p.label(), p) for p in profs]
        choices += [Choice("＋ Novo perfil…", "new"), Choice("← Voltar", BACK)]
        chosen = select("Perfis salvos:", choices)
        if chosen is None:
            return
        if chosen == "new":
            tui_edit_profile(store)
            continue
        action = select(f"{chosen.name}:", [Choice("Editar", "edit"), Choice("Remover", "rm"),
                                             Choice("← Voltar", BACK)])
        if action == "edit":
            tui_edit_profile(store, chosen)
        elif action == "rm" and confirm(f"Remover o perfil '{chosen.name}'? (o CSV fica intacto)", default=False):
            store["profiles"] = [p for p in store["profiles"] if p["name"] != chosen.name]
            if store.get("last_profile") == chosen.name:
                store["last_profile"] = None
            save_store(store)
            print(f"  🗑  perfil '{chosen.name}' removido")


def _validate_dt(v: str):
    return True if parse_dt(v) else "use YYYY-MM-DD ou 'YYYY-MM-DD HH:MM'"


def _validate_int(lo: int, hi: int):
    def check(v: str):
        return True if v.strip().isdigit() and lo <= int(v) <= hi else f"número entre {lo} e {hi}"
    return check


def tui_pick_query(store: dict) -> Query | None:
    week, month = preset_week(), preset_month()
    choices = [
        Choice(f"Última semana   {week.describe()}", "week"),
        Choice(f"Último mês      {month.describe()}", "month"),
        Choice("Personalizado…  (datas, quantidade de álbuns e colunas à escolha)", "custom"),
        Choice("← Voltar", BACK),
    ]
    kind = select("Qual período?", choices)
    if kind is None:
        return None
    if kind == "week":
        return week
    if kind == "month":
        return month

    last = store.get("last_custom") or {}
    start = text("Início (YYYY-MM-DD [HH:MM]):", default=last.get("start", week.start.strftime(DT_FMT)),
                 validate=_validate_dt)
    if start is None:
        return None
    end = text("Fim (YYYY-MM-DD [HH:MM]; só a data = dia inteiro):",
               default=last.get("end", dt.datetime.now().strftime(DT_FMT)), validate=_validate_dt)
    if end is None:
        return None
    rows = text("Linhas da grade:", default=str(last.get("rows", 5)), validate=_validate_int(1, 50))
    if rows is None:
        return None
    cols = text("Colunas da grade:", default=str(last.get("columns", 5)), validate=_validate_int(1, 50))
    if cols is None:
        return None
    print(f"  → grade {rows}×{cols} = {int(rows) * int(cols)} álbuns")
    mode = select("Ordenar por:", [Choice("Tempo de escuta (duração × plays)", "listening_time"),
                                   Choice("Número de scrobbles", "scrobbles")],
                  default=None)
    if mode is None:
        return None
    q = custom_query(start, end, int(rows), int(cols), mode)
    store["last_custom"] = {"start": start, "end": end, "rows": int(rows), "columns": int(cols), "mode": mode}
    save_store(store)
    return q


def tui_ask_ratings(albums: list[dict], ratings: dict[int, str], csv_path: str) -> None:
    pending = [i for i in range(len(albums)) if i not in ratings]
    if not pending:
        return
    print(f"\n  {len(pending)} álbum(ns) sem nota no CSV.")
    if not confirm("Quer dar as notas agora? (Enter pula um álbum; as notas vão pro CSV)", default=False):
        return

    def validate(v: str):
        return True if v.strip() == "" or parse_rating(v) else "1–10 (ex.: 8) ou estrelas (ex.: 3.5*); vazio pula"

    for n, i in enumerate(pending, 1):
        a = albums[i]
        raw = text(f"[{n}/{len(pending)}] {a['artist']} — {a['name']}  nota:", validate=validate)
        if raw is None:
            print("  (interrompido; o que já foi dado está salvo)")
            return
        rating = parse_rating(raw) if raw.strip() else None
        if rating:
            ratings[i] = rating
            engine.append_rym_rating(csv_path, a["artist"], a["name"], rating)
            print(f"      → {engine.rating_to_stars(rating)}  salvo")


def tui_generate(store: dict) -> None:
    prof = tui_pick_profile(store)
    if prof is None:
        return
    store["last_profile"] = prof.name
    save_store(store)
    q = tui_pick_query(store)
    if q is None:
        return
    print(f"\n  👤 {prof.name} (@{prof.lastfm})\n  📅 {q.describe()}")
    if not confirm("Gerar?", default=True):
        return
    r = run_query(store, prof, q, ask_ratings=tui_ask_ratings)
    print_summary(r)
    if r.paths and confirm("Abrir as imagens?", default=True):
        open_files([Path(p) for p in r.paths.values()])


def tui_open_latest(store: dict) -> None:
    profs = profiles(store)
    if not profs:
        print("  nenhum perfil ainda")
        return
    choices = [Choice(p.label(), p) for p in profs] + [Choice("← Voltar", BACK)]
    prof = select("De qual perfil?", choices)
    if prof is None:
        return
    paths = latest_paths(store, prof)
    if not paths:
        print(f"  nenhum grid gerado ainda para {prof.name}")
        return
    for k, p in paths.items():
        print(f"  {k:<6} {p}")
    open_files(list(paths.values()))


def tui_settings(store: dict) -> None:
    while True:
        key = api_key(store)
        masked = (key[:4] + "…" + key[-4:]) if len(key) > 8 else ("(não definida)" if not key else key)
        choice = select("Configurações:", [
            Choice(f"Chave da API do Last.fm   {masked}", "key"),
            Choice(f"Pasta de saída            {store['output_dir']}", "out"),
            Choice(f"Fonte CJK                 {store.get('cjk_font') or '(auto)'}", "font"),
            Choice("Testar conexão / diagnóstico", "doctor"),
            Choice(f"Abrir o arquivo de config ({CONFIG_PATH})", "cfg"),
            Choice("← Voltar", BACK),
        ])
        if choice is None:
            return
        if choice == "key":
            v = text("Chave da API do Last.fm (last.fm/api/account/create):", default=store.get("lastfm_api_key", ""))
            if v is not None:
                store["lastfm_api_key"] = v.strip()
        elif choice == "out":
            v = ask(questionary.path("Pasta de saída:", default=store["output_dir"], only_directories=True, style=STYLE))
            if v is not None:
                store["output_dir"] = str(Path(v).expanduser().resolve())
        elif choice == "font":
            v = ask(questionary.path("Arquivo .otf/.ttf com suporte CJK (vazio = auto):",
                                     default=store.get("cjk_font", ""), style=STYLE))
            if v is not None:
                store["cjk_font"] = v.strip()
        elif choice == "doctor":
            doctor(store)
            continue
        elif choice == "cfg":
            open_files([CONFIG_PATH])
            continue
        save_store(store)
        print("  ✅ salvo")


def tui_first_run(store: dict) -> None:
    print("  Primeira vez por aqui. Vamos configurar o básico.\n")
    if not api_key(store):
        v = text("Chave da API do Last.fm (grátis em last.fm/api/account/create):", validate=_validate_nonempty)
        if v is None:
            return
        store["lastfm_api_key"] = v.strip()
        save_store(store)
    if not store["profiles"]:
        tui_edit_profile(store)


def tui() -> int:
    store = load_store()
    banner(store)
    if not api_key(store) or not store["profiles"]:
        tui_first_run(store)
    while True:
        action = select("O que você quer fazer?", [
            Choice("🎨  Gerar grid", "run"),
            Choice("👤  Perfis", "profiles"),
            Choice("🖼   Abrir os últimos grids", "open"),
            Choice("⚙️   Configurações", "settings"),
            Choice("Sair", "quit"),
        ])
        if action in (None, "quit"):
            print("  até a próxima 👋")
            return 0
        {"run": tui_generate, "profiles": tui_profiles, "open": tui_open_latest,
         "settings": tui_settings}[action](store)


# ── diagnóstico ──────────────────────────────────────────────

def doctor(store: dict) -> int:
    ok = True

    def check(label: str, good: bool, detail: str = "") -> None:
        nonlocal ok
        ok &= good
        print(f"  {'✓' if good else '✗'}  {label}{(': ' + detail) if detail else ''}")

    print(f"\n  config: {CONFIG_PATH}")
    out = Path(store["output_dir"]).expanduser()
    check("pasta de saída gravável", os.access(out if out.exists() else out.parent, os.W_OK), str(out))
    key = api_key(store)
    check("chave da API do Last.fm definida", bool(key))
    profs = profiles(store)
    check("ao menos um perfil", bool(profs))
    for p in profs:
        check(f"CSV de {p.name}", True, str(p.csv_path) + ("" if p.csv_path.is_file() else " (ainda não existe; criado na 1ª nota)"))
    if key:
        try:
            for p in profs:
                data = engine._lastfm_get(key, {"method": "user.getInfo", "user": p.lastfm})
                if "error" in data:
                    check(f"Last.fm @{p.lastfm}", False, str(data.get("message")))
                else:
                    check(f"Last.fm @{p.lastfm}", True, f"{data.get('user', {}).get('playcount', '?')} scrobbles")
        except requests.RequestException as e:
            check("Last.fm alcançável", False, str(e))
    with contextlib.redirect_stdout(io.StringIO()):
        engine.configure(cjk_font=store.get("cjk_font") or None)
        fonts = engine.load_fonts()
    check("fonte CJK (japonês)", engine._font_renders_japanese(fonts["cjk_md"]), store.get("cjk_font") or "auto")
    print("\n  tudo certo ✨\n" if ok else "\n  há problemas acima\n")
    return 0 if ok else 1


# ── modo não-interativo (subcomandos) ────────────────────────

def _query_from_args(store: dict, args: argparse.Namespace) -> Query:
    q = preset_month() if args.preset == "month" else preset_week()
    if args.since or args.until:
        if not (args.since and args.until):
            fail("janela personalizada precisa de --since e --until")
        rows, cols = parse_grid(args.grid) if args.grid else (q.rows, q.columns)
        return custom_query(args.since, args.until, rows, cols, args.mode or "listening_time")
    if args.grid:
        q.rows, q.columns = parse_grid(args.grid)
    if args.mode:
        q.mode = args.mode
    return q


def _resolve_profiles(store: dict, names: list[str]) -> list[Profile]:
    profs = profiles(store)
    if not profs:
        fail("nenhum perfil salvo; rode `semaninha` para criar um")
    if not names or names == ["all"]:
        return profs
    out: list[Profile] = []
    for n in names:
        p = find_profile(store, n)
        if p is None:
            fail(f"perfil desconhecido: {n!r}. Disponíveis: " + ", ".join(x.name for x in profs))
        if p not in out:
            out.append(p)
    return out


def cmd_run(args: argparse.Namespace, store: dict, dry_run: bool = False) -> int:
    q = _query_from_args(store, args)
    results = [run_query(store, p, q, quiet=args.quiet, to_stderr=args.json, dry_run=dry_run)
               for p in _resolve_profiles(store, args.profiles)]
    if args.json:
        print(json.dumps([r.to_json() for r in results], ensure_ascii=False, indent=2))
    elif dry_run:
        for r in results:
            print(f"\n━━ {r.profile.name} · {r.query.describe()}")
            if r.error:
                print(f"  ❌ {r.error}")
            elif not r.albums:
                print("  (sem álbuns no período)")
            elif r.unmatched:
                print(f"  {len(r.unmatched)} sem rating ({len(r.ratings)}/{len(r.albums)} ok):")
                for u in r.unmatched:
                    print(f"    – {u['artist']} — {u['album']}")
                print(f'\n  dica: semaninha rate {r.profile.name} "<artista>" "<álbum>" <1-10>')
            else:
                print(f"  todos os {len(r.albums)} com rating ✨")
    else:
        for r in results:
            print_summary(r)
    if not dry_run and getattr(args, "open", False):
        open_files([Path(p) for r in results for p in r.paths.values()])
    return 1 if any(r.error for r in results) else 0


def cmd_rate(args: argparse.Namespace, store: dict) -> int:
    prof = _resolve_profiles(store, [args.profile])[0]
    rating = parse_rating(args.rating)
    if rating is None:
        fail(f"nota inválida: {args.rating!r} (1–10 em passos de 0.5, ou estrelas como 4.5*)")
    engine.append_rym_rating(str(prof.csv_path), args.artist, args.album, rating)
    print(f"💾  {prof.name}: {args.artist} — {args.album} → {rating}/10 {engine.rating_to_stars(rating)}"
          f"  ({prof.csv_path.name})")
    return 0


def cmd_profiles(args: argparse.Namespace, store: dict) -> int:
    profs = profiles(store)
    if args.json:
        print(json.dumps([{**asdict(p), "csv_exists": p.csv_path.is_file()} for p in profs],
                         ensure_ascii=False, indent=2))
        return 0
    print(f"config: {CONFIG_PATH}\nsaída:  {store['output_dir']}\n")
    for p in profs:
        print(f"  {p.label()}  {'✓' if p.csv_path.is_file() else '(CSV ainda não existe)'}")
    return 0


def cmd_latest(args: argparse.Namespace, store: dict) -> int:
    rows = [(p, latest_paths(store, p, args.kind)) for p in _resolve_profiles(store, args.profiles)]
    if args.json:
        print(json.dumps({p.name: {k: str(v) for k, v in paths.items()} for p, paths in rows},
                         ensure_ascii=False, indent=2))
        return 0
    for p, paths in rows:
        print(f"\n━━ {p.name}")
        if not paths:
            print("  (nenhum grid gerado ainda)")
            continue
        for k in ("timed", "plain"):
            if k in paths:
                print(f"  {k:<6} {paths[k]}")
    return 0


def cmd_open(args: argparse.Namespace, store: dict) -> int:
    files = [f for p in _resolve_profiles(store, args.profiles)
             for f in latest_paths(store, p, args.kind).values()]
    for f in files:
        print(f"  📂 {f}")
    open_files(files)
    return 0 if files else 1


def cmd_presets(args: argparse.Namespace, store: dict) -> int:
    for q in (preset_week(), preset_month()):
        print(f"  {q.kind:<6} {q.describe()}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="semaninha",
        description="Grids de álbuns do Last.fm com notas do RateYourMusic. Sem argumentos: menu interativo.",
    )
    sub = p.add_subparsers(dest="cmd", metavar="COMANDO")

    def run_opts(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("profiles", nargs="*", metavar="PERFIL", help="perfil(is) salvos; vazio = todos")
        sp.add_argument("--preset", choices=["week", "month"], default="week",
                        help="week = sex 11h → sex 11h, 5×5 (padrão); month = dia 1 11h → dia 1 11h, 8×8")
        sp.add_argument("--since", metavar="'YYYY-MM-DD[ HH:MM]'", help="início de janela personalizada")
        sp.add_argument("--until", metavar="'YYYY-MM-DD[ HH:MM]'", help="fim (exclusivo; só a data = dia inteiro)")
        sp.add_argument("--grid", metavar="LxC", help="grade em LINHASxCOLUNAS (ex.: 4x6); padrão: o do preset")
        sp.add_argument("-m", "--mode", choices=["listening_time", "scrobbles"])
        sp.add_argument("--json", action="store_true", help="saída em JSON (logs vão para stderr)")
        sp.add_argument("-q", "--quiet", action="store_true", help="esconde os logs do gerador")

    r = sub.add_parser("run", help="gera os grids sem menu")
    run_opts(r)
    r.add_argument("-o", "--open", action="store_true", help="abre as imagens ao terminar")
    r.set_defaults(func=cmd_run)

    u = sub.add_parser("unmatched", help="lista os álbuns do período sem nota (não gera imagem)")
    run_opts(u)
    u.set_defaults(func=lambda a, s: cmd_run(a, s, dry_run=True))

    t = sub.add_parser("rate", help="salva uma nota no CSV de um perfil",
                       description='Ex.: semaninha rate soda "Fishmans" "Long Season" 10\n'
                                   '     semaninha rate lise "Blur" "Parklife" 4.5*   (estrelas → 9)',
                       formatter_class=argparse.RawDescriptionHelpFormatter)
    t.add_argument("profile", metavar="PERFIL")
    t.add_argument("artist", metavar="ARTISTA")
    t.add_argument("album", metavar="ÁLBUM")
    t.add_argument("rating", metavar="NOTA", help="1–10 (escala RYM) ou estrelas com * (ex.: 3.5*)")
    t.set_defaults(func=cmd_rate)

    pr = sub.add_parser("profiles", help="lista os perfis salvos")
    pr.add_argument("--json", action="store_true")
    pr.set_defaults(func=cmd_profiles)

    for name, fn, hlp in (("latest", cmd_latest, "caminhos dos últimos grids"),
                          ("open", cmd_open, "abre os últimos grids no visualizador")):
        sp = sub.add_parser(name, help=hlp)
        sp.add_argument("profiles", nargs="*", metavar="PERFIL")
        sp.add_argument("--kind", choices=["weekly", "monthly", "custom"], help="padrão: o mais recente de qualquer tipo")
        if name == "latest":
            sp.add_argument("--json", action="store_true")
        sp.set_defaults(func=fn)

    sub.add_parser("presets", help="mostra as janelas dos presets de hoje").set_defaults(func=cmd_presets)
    sub.add_parser("doctor", help="checa config, CSVs, API do Last.fm e fonte CJK").set_defaults(
        func=lambda a, s: doctor(s))
    sub.add_parser("config", help="imprime o caminho do arquivo de config").set_defaults(
        func=lambda a, s: print(CONFIG_PATH) or 0)
    return p


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        return tui()
    args = build_parser().parse_args(argv)
    if not hasattr(args, "func"):
        return tui()
    try:
        return int(args.func(args, load_store()) or 0)
    except KeyboardInterrupt:
        print("\n  interrompido", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
