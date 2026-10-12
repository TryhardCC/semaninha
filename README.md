# semaninha

Collage PNG dos álbuns mais ouvidos num período, cruzando scrobbles do **Last.fm** com notas do **Rate Your Music (RYM)**. Vem com uma CLI interativa (menus com setas) e um modo não-interativo para automação.

---

## Instalação

Não precisa ter Python instalado: o instalador baixa o [uv](https://docs.astral.sh/uv/), que cuida do Python e das dependências sozinho.

**Recebeu o zip?**
1. Descompacte.
2. **macOS:** dois cliques em `Instalar.command`. Se o macOS disser que "não pode ser aberto", clique com o botão direito → *Abrir*.
   **Windows:** botão direito em `install.ps1` → *Executar com o PowerShell*.
3. Vai aparecer um atalho **Semaninha** na Mesa. Dois cliques nele abre o programa. Também dá pra abrir um Terminal e digitar `semaninha`.

**Pelo Terminal (macOS/Linux), uma linha:**

```bash
curl -fsSL https://raw.githubusercontent.com/TryhardCC/semaninha/main/install.sh | sh
```

Na primeira vez o programa pede a chave da API do Last.fm (grátis, 1 minuto em last.fm/api/account/create) e cria o primeiro perfil.

**Para quem vai mexer no código** (instalação editável, mudanças nos `.py` valem na hora):

```bash
cd ~/semaninha/semaninha
uv tool install -e .
```

**Para mandar pra alguém:** `sh empacotar.sh` gera `dist/semaninha.zip` só com o necessário (sem CSVs, imagens ou chaves).

---

## Uso interativo

```bash
semaninha
```

Abre o menu. Na primeira vez ele pede a chave da API do Last.fm (grátis em last.fm/api/account/create) e cria o primeiro perfil.

```
? O que você quer fazer?
 » Gerar grid
   Perfis
   Abrir os últimos grids
   Configurações
   Sair
```

**Gerar grid** → escolhe o perfil → escolhe o período → confirma. Se sobrarem álbuns sem nota, pergunta se quer dar as notas na hora (vão pro CSV do perfil). No fim oferece abrir as imagens.

**Perfis** guardam: apelido, usuário do Last.fm, chave/usuário do RYM e o caminho do CSV exportado do RYM. O último perfil usado já vem selecionado.

**Períodos**

| Preset | Janela de scrobbles | Grade |
|---|---|---|
| Última semana | 11h da penúltima sexta → 11h da última sexta (hoje conta, se for sexta) | 5×5 |
| Último mês | 11h do dia 1 do mês passado → 11h do dia 1 deste mês | 8×8 |
| Personalizado | qualquer início/fim (`YYYY-MM-DD` ou `YYYY-MM-DD HH:MM`) | qualquer **linhas × colunas** |

No personalizado também dá pra escolher a ordenação: tempo de escuta (duração × plays, padrão) ou número de scrobbles. Os presets usam tempo de escuta e geram duas imagens: `_timed.png` (com o tempo ouvido na capa) e `.png` (sem).

Nos menus: `↑↓` move, `Enter` escolhe, `Ctrl-C` volta.

---

## Uso não-interativo (scripts, agentes)

```bash
semaninha run                      # todos os perfis, preset da semana
semaninha run soda --preset month  # um perfil, preset do mês
semaninha run lise --since 2026-10-01 --until "2026-10-05 11:00" --grid 3x7 -m scrobbles
semaninha run --json -q            # resultado em JSON (logs no stderr)
semaninha run -o                   # abre as imagens ao terminar

semaninha unmatched [PERFIL…]      # só lista os álbuns sem nota (não gera imagem)
semaninha rate soda "Fishmans" "Long Season" 10      # salva nota no CSV (1–10)
semaninha rate lise "Blur" "Parklife" 4.5*           # ou em estrelas (4.5* → 9)
semaninha latest [--json]          # caminhos dos últimos grids
semaninha open soda                # abre os últimos grids
semaninha presets                  # mostra as janelas dos presets de hoje
semaninha profiles                 # lista os perfis
semaninha doctor                   # checa config, CSVs, Last.fm e fonte CJK
semaninha config                   # caminho do arquivo de configuração
```

`--grid` é sempre `LINHASxCOLUNAS` (ex.: `4x6` = 24 álbuns). Um perfil pode ser chamado pelo apelido, pelo usuário do Last.fm ou pelo do RYM.

---

## Onde ficam as coisas

- **Configuração e perfis:** `~/.config/semaninha/config.json` (ou `$SEMANINHA_CONFIG`). Chave da API, pasta de saída, fonte CJK, listas de ignorados (`ignore.artists`, `ignore.albums`, `ignore.pairs`) e perfis. A variável `LASTFM_API_KEY` no shell tem prioridade sobre a chave do arquivo.
- **Imagens:** `out/` dentro do projeto (configurável), nomeadas `weekly_grid_<user>_<timestamp>.png`, `monthly_grid_…` ou `custom_grid_…`.
- **CSV do RYM:** exporte em *Settings → Export Data → Export ratings* e aponte o perfil pra ele. Notas dadas pela CLI são anexadas nesse arquivo.

---

## Fonte CJK (japonês/chinês/coreano)

Se caracteres aparecerem como `□`:

```bash
brew install --cask font-noto-sans-cjk-japanese
```

Depois aponte o arquivo em *Configurações → Fonte CJK* (vazio = auto-detectar). `python find_cjk_font.py` ajuda a achar fontes instaladas.

---

## Arquivos

- `semaninha.py` — a CLI (menu interativo + subcomandos).
- `weekly_music_grid.py` — o motor: Last.fm, matching com o RYM, montagem da imagem. Ainda roda sozinho (`python weekly_music_grid.py --help`) com as flags antigas.
