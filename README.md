# 3D Printing Intelligence Radar — fetch + publish (V1)

Ingest vrstva pro denní/týdenní koncentrátor 3D printing zpráv, plus jednoduchá
statická stránka publikovaná na GitHub Pages. V1 pokrývá zdroje se stavem
`READY` (přímé RSS/Atom) a čtyři ručně napojené CUSTOM adaptéry (arXiv,
GitHub Discovery, Klipper doc-changes). WEB/BRIDGE/QUERY zdroje jsou ve v1
záměrně jen "SKIP" — jde o fázi 2 (scraping, translation bridge pro CN/KO,
custom research query adaptéry). AI processing stage (relevance/novelty
scoring, EVENT dedup, CZ překlad, kategorizace) taky zatím neexistuje —
publikovaná stránka je syrový, ale čitelný přehled toho, co fetch stage
posbírala, ne ještě plnohodnotný denní digest podle původní architektury.

## Spuštění

```bash
pip install -r requirements.txt
python scripts/fetch_3dprinting.py --hours 48
```

Volitelné parametry:

- `--sources` — cesta k CSV master listu (default `config/3d_printing_sources.csv`)
- `--output` — kam zapsat výstupní JSON (default `output/3dprinting_raw.json`)
- `--state` — stavový soubor pro health/anomaly tracking (default `state/source_health.json`)
- `--priority` — které priority zahrnout, např. `A+,A,B` (default) nebo `A+,A,B,C`

Pro GitHub adaptéry (Discovery, Klipper) nastav `GITHUB_TOKEN` v prostředí —
bez tokenu narazíš rychle na rate limit anonymních GitHub API požadavků.

```bash
export GITHUB_TOKEN=ghp_...
```

## Co dělá endpoint_status v CSV

| Stav | Chování skriptu v1 | Co znamená |
|---|---|---|
| `READY` | Stáhne a zpracuje jako RSS/Atom feed. | `feed_or_endpoint` je ověřený, strojově čitelný feed. |
| `VERIFY` | SKIP, jen log. | Feed URL existuje, ale potřebuje ruční ověření/opravu (viz `notes`) — momentálně `Fabbaloo` (feed vrací HTTP 403). |
| `WEB` | SKIP, jen log. | Zdroj nemá feed, potřebuje web-scraping/change-detection adaptér (fáze 2) — hlavně výrobci (Prusa má výjimku, ten feed má). |
| `BRIDGE` | SKIP, jen log. | Jako WEB, navíc CN/KO obsah — potřebuje i překladový bridge (fáze 2). |
| `QUERY` | SKIP, jen log. | Potřebuje vlastní search/query adaptér (NIST, ORNL, Fraunhofer, Nature, Siemens, Elsevier). |
| `CUSTOM` | Zpracuje JEN pokud název zdroje odpovídá jednomu z jmenných handlerů v `main()` (`arXiv...`, `GitHub Discovery Radar`, `Klipper`). Jinak SKIP s upozorněním, že routing chybí. | Vlastní adaptér mimo obecný RSS/Atom flow. |
| `BENCHMARK` | SKIP (a navíc `enabled=False`). | Kontrolní zdroj (Today in 3D Printing), ne ingest. |

**Důležité:** routing v `main()` se řídí primárně `endpoint_status`, ne
textem v `ingest_type`. Předchozí verze parsovala `ingest_type` (řetězce
jako "RSS/Search") a to způsobilo tichý bug — `Nature Portfolio Search` má
`ingest_type=RSS/Search`, ale `feed_or_endpoint` je HTML search stránka, ne
feed; skript by ji tiše zkusil parsovat jako RSS a vracel by 0 položek bez
chyby. Status-based routing to řeší: `QUERY` zdroje se do `fetch_feed()`
vůbec nedostanou, ať `ingest_type` říká cokoliv.

## Známé mezery / co ověřit před ostrým nasazením

- **Fabbaloo** (`SRC008`) — feed vrací HTTP 403 (ověřeno opakovaně
  19.8.2026). Status je teď `VERIFY`, ne `READY`. Najít náhradní endpoint
  nebo řešit blokaci bota (User-Agent, IP), pak přepnout zpět.
- **3D Science Valley** (`SRC030`) — CSV URL `3dsciencevalley.com` se mi
  nepodařilo z mého prostředí ověřit jako dosažitelnou. Anglická verze
  `en.51shape.com` byla ověřena funkční 18.8.2026 — zkontroluj ručně, než
  stavíš BRIDGE adaptér, jestli je to tatáž organizace/aktuální doména.
- **arXiv, export.arxiv.org** a **api.github.com** jsem z tohoto vývojového
  prostředí nemohl live protestovat end-to-end (síťová omezení sandboxu +
  robots.txt na fetch nástroji) — kód prošel syntaktickou kontrolou a
  logickým testem CSV routingu (`47` zdrojů načteno, `14` READY, arXiv
  query-string se správně sestavuje s `all:` prefixem na obou frázích), ale
  doporučuju první ostrý běh spustit tam, kde to skutečně poběží (GitHub
  Actions / vlastní stroj) a zkontrolovat stdout log.
- **GitHub Discovery** teď filtruje forky a repozitáře pod `GITHUB_MIN_STARS`
  (default 5) — bez tohoto filtru dotaz `3d-printing` vrací zjevný SEO-spam
  na vrchu výsledků (ověřeno živě). Práh je nastavitelný jako konstanta v
  souboru, případně přidat jako CLI parametr, až bude potřeba ladit.

## Anomaly watch (state/source_health.json)

Skript si po každém běhu pamatuje, jestli zdroj typicky vrací něco (`ever_nonzero`)
a kolik běhů po sobě vrátil 0 (`consecutive_zero`). Pokud zdroj, který dřív
běžně reportoval obsah, vrátí 0 třikrát po sobě (`ANOMALY_THRESHOLD`), skript
to na konci běhu vypíše pod `⚠ ANOMALY WATCH` — to je způsob, jak si všimnout
mrtvého feedu (jako Fabbaloo) bez ručního čtení celého logu každý den.

## Struktura výstupního záznamu

Každá položka v `output/3dprinting_raw.json` má normalizované pole
(`source`, `title`, `link`, `published`, `summary`, `translation_required`,
...) a prázdná pole pro další AI krok (`category`, `relevance_score`,
`novelty_score`, `event_id`, `processed`) — ten navazuje mimo tento skript.

## Publikace na GitHub Pages

Po nahrání celého obsahu tohoto balíčku (včetně `.github/workflows/daily-radar.yml`)
do repozitáře stačí jednorázově:

1. V repu jít do **Settings → Pages** a jako **Source** vybrat **GitHub
   Actions** (ne "Deploy from branch" — ten starý mechanismus se nepoužívá).
2. Spustit workflow **Daily 3D Printing Radar** poprvé ručně přes záložku
   **Actions → Daily 3D Printing Radar → Run workflow** (nebo počkat na
   plánovaný běh).
3. Po prvním úspěšném běhu se URL stránky objeví v **Settings → Pages**
   (tvar `https://<uživatel>.github.io/<repo>/`) a taky ve výstupu joblu
   (`steps.deployment.outputs.page_url`).

Workflow běží denně (`cron: "30 5 * * *"`, tedy cca 7:30 SELČ/6:30 SEČ podle
letního/zimního času — GitHub Actions cron je vždy v UTC) a taky ručně přes
**workflow_dispatch**. Jeden běh: spustí `fetch_3dprinting.py`, commitne
zpátky `state/source_health.json` (jinak by anomaly tracking mezi běhy
neplatil), vygeneruje `site/` přes `build_site.py` a nasadí ho jako GitHub
Pages přes `actions/deploy-pages`.

**Poznámka k oprávněním:** workflow má explicitní `permissions: contents:
write` — to stačí, i kdyby repo mělo defaultně nastavená práva GITHUB_TOKENu
na read-only (Settings → Actions → General → Workflow permissions),
protože explicitní blok v YAML má přednost pro tenhle konkrétní job.

`output/3dprinting_raw.json` a `site/` se necommitují do gitu (jsou v
`.gitignore`) — jsou to build artefakty, generují se při každém běhu znovu a
publikují se přes Pages artifact. Jediné, co se z generovaného obsahu
commituje zpátky, je `state/source_health.json`, protože ten musí přežít
mezi jednotlivými dny.

## Co zbývá do fáze 2

1. Web-scraping/change-detection adaptér pro `WEB` zdroje (výrobci).
2. Scraping + CN/KO→CZ translation bridge pro `BRIDGE` zdroje (Nanjixiong,
   3D Science Valley, 3DBank, 3D Printing Times Korea).
3. Custom query adaptéry pro `QUERY` zdroje (NIST, ORNL, Fraunhofer, Nature,
   Siemens, Elsevier) — každý má jiný způsob, jak se k novým položkám dostat.
4. AI processing stage: levná klasifikace/relevance nejdřív, pak EVENT
   dedup/clustering, teprve pak plný překlad + shrnutí jen pro zdroj-events,
   co filtrem projdou (viz diskuze k architektuře).
5. Až AI processing stage existuje, `build_site.py` přepsat/rozšířit tak, ať
   renderuje skutečný denní digest (TOP5 + kategorie HW/SW/AI/Materials/
   Industrial/Research/China/Korea), ne jen chronologický přehled podle
   typu ingestu jako teď.
