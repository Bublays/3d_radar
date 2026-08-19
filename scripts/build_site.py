"""
3D Printing Intelligence Radar — static site build (V1, "raw" view)

Vezme output/3dprinting_raw.json (výstup fetch_3dprinting.py) a state
soubor s anomaly-trackingem a vygeneruje samostatnou statickou stránku
(site/index.html) pro GitHub Pages.

DŮLEŽITÉ: AI processing stage (relevance/novelty score, EVENT dedup,
CZ překlad, kategorizace do HW/SW/AI/Materials/...) ve v1 NEEXISTUJE.
Tahle stránka je syrový, ale čitelný přehled toho, co fetch stage
posbírala — seskupený podle typu ingestu a seřazený podle data publikace.
Až přibude AI processing, nahradí/doplní tuhle stránku plnohodnotný denní
digest (TOP5 + kategorie), jak jsme navrhli v architektuře.
"""
import argparse
import csv
import html
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_INPUT = "output/3dprinting_raw.json"
DEFAULT_SOURCES = "config/3d_printing_sources.csv"
DEFAULT_STATE = "state/source_health.json"
DEFAULT_OUTPUT_DIR = "site"
ANOMALY_THRESHOLD = 3  # musí odpovídat konstantě ve fetch_3dprinting.py

RAW_TYPE_META = {
    "rss_atom": {"label": "📰 RSS / Atom (média, vendoři, OSS release feedy)", "slot": 1},
    "arxiv": {"label": "🔬 arXiv research papers", "slot": 2},
    "github_discovery": {"label": "🐙 GitHub Discovery (nové/rostoucí repozitáře)", "slot": 3},
    "github_commit": {"label": "🛠 Klipper změny (Config/Releases)", "slot": 4},
}
RAW_TYPE_ORDER = ["rss_atom", "arxiv", "github_discovery", "github_commit"]

# Kategorický slot z dataviz palety (pevné pořadí, identita podle raw_type) —
# viz references/palette.md v dataviz skillu.
SLOT_COLORS_LIGHT = {1: "#2a78d6", 2: "#eb6834", 3: "#1baf7a", 4: "#eda100"}
SLOT_COLORS_DARK = {1: "#3987e5", 2: "#d95926", 3: "#199e70", 4: "#c98500"}

# Ordinální (sekvenční, jedna hue) škála pro prioritu — A+ nejtmavší/nejsytější.
PRIORITY_ORDER = ["A+", "A", "B", "C"]
PRIORITY_COLOR_LIGHT = {"A+": "#184f95", "A": "#2a78d6", "B": "#6da7ec", "C": "#b7d3f6"}
PRIORITY_COLOR_DARK = {"A+": "#184f95", "A": "#3987e5", "B": "#5598e7", "C": "#86b6ef"}


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--input", default=DEFAULT_INPUT, help="Vstupní JSON z fetch_3dprinting.py.")
    p.add_argument("--sources", default=DEFAULT_SOURCES, help="CSV master list (pro anomaly/jméno zdroje).")
    p.add_argument("--state", default=DEFAULT_STATE, help="Stavový soubor s health/anomaly trackingem.")
    p.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help="Kam zapsat statickou stránku.")
    return p.parse_args()


def load_json(path: str, default):
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def load_source_names(csv_path: str) -> dict:
    p = Path(csv_path)
    names = {}
    if not p.exists():
        return names
    with p.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("source_id"):
                names[row["source_id"]] = row.get("name", row["source_id"])
    return names


def build_anomalies(state: dict, source_names: dict) -> list:
    out = []
    for source_id, entry in state.items():
        if entry.get("ever_nonzero") and entry.get("consecutive_zero", 0) >= ANOMALY_THRESHOLD:
            out.append(
                {
                    "source_id": source_id,
                    "name": source_names.get(source_id, source_id),
                    "consecutive_zero": entry["consecutive_zero"],
                    "last_run": entry.get("last_run", ""),
                }
            )
    out.sort(key=lambda x: -x["consecutive_zero"])
    return out


def esc(text) -> str:
    return html.escape(str(text or ""), quote=True)


def fmt_date(iso_str: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.strftime("%d.%m.%Y %H:%M UTC")
    except Exception:
        return esc(iso_str)


def render_card(item: dict) -> str:
    priority = item.get("priority") or "?"
    prio_l = PRIORITY_COLOR_LIGHT.get(priority, "#898781")
    prio_d = PRIORITY_COLOR_DARK.get(priority, "#898781")
    translation = item.get("translation_required")
    flags = []
    if translation:
        flags.append('<span class="flag flag-translate">potřebuje překlad</span>')
    lang = item.get("language") or ""
    country = item.get("country") or ""
    summary = item.get("summary") or ""
    if len(summary) > 280:
        summary = summary[:277].rstrip() + "…"
    extra = item.get("extra") or {}
    extra_bits = []
    if "stars" in extra:
        extra_bits.append(f'★ {extra.get("stars", 0)}')
    if extra.get("matched_query"):
        extra_bits.append(f'dotaz: {esc(extra["matched_query"])}')
    extra_line = f'<div class="card-extra">{" · ".join(extra_bits)}</div>' if extra_bits else ""

    return f"""
    <article class="card">
      <div class="card-top">
        <span class="badge prio" style="--c-light:{prio_l}; --c-dark:{prio_d}">{esc(priority)}</span>
        <span class="card-source">{esc(item.get("source"))}</span>
        <span class="card-meta">{esc(country)}{" · " if country and lang else ""}{esc(lang)}</span>
        <span class="card-date">{fmt_date(item.get("published", ""))}</span>
      </div>
      <a class="card-title" href="{esc(item.get("link"))}" target="_blank" rel="noopener noreferrer">{esc(item.get("title"))}</a>
      {f'<p class="card-summary">{esc(summary)}</p>' if summary else ""}
      {extra_line}
      {"".join(flags)}
    </article>
    """


def render_section(raw_type: str, items: list) -> str:
    meta = RAW_TYPE_META.get(raw_type, {"label": raw_type, "slot": 1})
    slot = meta["slot"]
    cards = "\n".join(render_card(i) for i in items)
    return f"""
    <section class="group" data-rawtype="{esc(raw_type)}">
      <h2 class="group-title">
        <span class="dot" style="--c-light:{SLOT_COLORS_LIGHT.get(slot, '#898781')}; --c-dark:{SLOT_COLORS_DARK.get(slot, '#898781')}"></span>
        {esc(meta["label"])} <span class="group-count">({len(items)})</span>
      </h2>
      <div class="cards">
        {cards}
      </div>
    </section>
    """


def render_anomaly_banner(anomalies: list) -> str:
    if not anomalies:
        return ""
    rows = "\n".join(
        f'<li><strong>{esc(a["name"])}</strong> — 0 položek {a["consecutive_zero"]}x po sobě '
        f'(poslední běh {fmt_date(a["last_run"])})</li>'
        for a in anomalies
    )
    return f"""
    <div class="anomaly-banner">
      <strong>⚠ ANOMALY WATCH</strong> — tyhle zdroje dřív běžně vracely obsah, teď hlásí 0:
      <ul>{rows}</ul>
    </div>
    """


def render_stat_tile(label: str, value, color_light: str, color_dark: str) -> str:
    return f"""
    <div class="stat-tile">
      <div class="stat-value" style="--c-light:{color_light}; --c-dark:{color_dark}">{value}</div>
      <div class="stat-label">{esc(label)}</div>
    </div>
    """


def build_html(items: list, anomalies: list, generated_at: str) -> str:
    by_type = {}
    for item in items:
        by_type.setdefault(item.get("raw_type", "other"), []).append(item)
    for lst in by_type.values():
        lst.sort(key=lambda x: x.get("published", ""), reverse=True)

    priority_counts = {}
    for item in items:
        p = item.get("priority") or "?"
        priority_counts[p] = priority_counts.get(p, 0) + 1

    stat_tiles = [render_stat_tile("položek celkem", len(items), "#0b0b0b", "#ffffff")]
    for raw_type in RAW_TYPE_ORDER:
        meta = RAW_TYPE_META[raw_type]
        slot = meta["slot"]
        count = len(by_type.get(raw_type, []))
        stat_tiles.append(
            render_stat_tile(meta["label"].split(" ", 1)[1], count, SLOT_COLORS_LIGHT[slot], SLOT_COLORS_DARK[slot])
        )

    sections = "\n".join(
        render_section(raw_type, by_type[raw_type]) for raw_type in RAW_TYPE_ORDER if by_type.get(raw_type)
    )
    other_types = [rt for rt in by_type if rt not in RAW_TYPE_ORDER]
    sections += "\n".join(render_section(rt, by_type[rt]) for rt in other_types)

    return f"""<!DOCTYPE html>
<html lang="cs">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>3D Printing Intelligence Radar — denní přehled</title>
<style>
  :root {{
    color-scheme: light;
    --surface-1: #fcfcfb;
    --page: #f9f9f7;
    --text-primary: #0b0b0b;
    --text-secondary: #52514e;
    --muted: #898781;
    --gridline: #e1e0d9;
    --border: rgba(11,11,11,0.10);
  }}
  @media (prefers-color-scheme: dark) {{
    :root:where(:not([data-theme="light"])) {{
      color-scheme: dark;
      --surface-1: #1a1a19;
      --page: #0d0d0d;
      --text-primary: #ffffff;
      --text-secondary: #c3c2b7;
      --muted: #898781;
      --gridline: #2c2c2a;
      --border: rgba(255,255,255,0.10);
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    background: var(--page);
    color: var(--text-primary);
  }}
  header {{
    padding: 32px 24px 16px;
    max-width: 1100px;
    margin: 0 auto;
  }}
  header h1 {{ margin: 0 0 4px; font-size: 22px; }}
  header .subtitle {{ color: var(--text-secondary); font-size: 14px; margin: 0 0 4px; }}
  header .disclaimer {{
    color: var(--muted); font-size: 13px; margin-top: 8px;
    border-left: 3px solid var(--gridline); padding-left: 10px;
  }}
  .anomaly-banner {{
    max-width: 1100px; margin: 12px auto; padding: 12px 16px;
    background: var(--surface-1); border: 1px solid var(--border);
    border-left: 3px solid #ec835a; border-radius: 8px; font-size: 13px;
  }}
  .anomaly-banner ul {{ margin: 6px 0 0; padding-left: 18px; }}
  .stats {{
    max-width: 1100px; margin: 16px auto; padding: 0 24px;
    display: flex; flex-wrap: wrap; gap: 12px;
  }}
  .stat-tile {{
    background: var(--surface-1); border: 1px solid var(--border);
    border-radius: 10px; padding: 10px 16px; min-width: 110px;
  }}
  .stat-value {{
    font-size: 22px; font-weight: 700;
    color: var(--c-light);
  }}
  @media (prefers-color-scheme: dark) {{ .stat-value {{ color: var(--c-dark); }} }}
  :root[data-theme="dark"] .stat-value {{ color: var(--c-dark); }}
  .stat-label {{ font-size: 12px; color: var(--text-secondary); }}
  #filter-box {{
    max-width: 1100px; margin: 0 auto 8px; padding: 0 24px;
  }}
  #search {{
    width: 100%; max-width: 360px; padding: 8px 12px; font-size: 14px;
    border: 1px solid var(--border); border-radius: 8px;
    background: var(--surface-1); color: var(--text-primary);
  }}
  main {{ max-width: 1100px; margin: 0 auto; padding: 8px 24px 48px; }}
  .group {{ margin-top: 28px; }}
  .group-title {{
    font-size: 16px; display: flex; align-items: center; gap: 8px;
    border-bottom: 1px solid var(--gridline); padding-bottom: 8px;
  }}
  .group-count {{ color: var(--muted); font-weight: 400; font-size: 13px; }}
  .dot {{
    width: 10px; height: 10px; border-radius: 50%; background: var(--c-light); display: inline-block;
  }}
  @media (prefers-color-scheme: dark) {{ .dot {{ background: var(--c-dark); }} }}
  :root[data-theme="dark"] .dot {{ background: var(--c-dark); }}
  .cards {{ display: grid; gap: 10px; margin-top: 12px; }}
  .card {{
    background: var(--surface-1); border: 1px solid var(--border);
    border-radius: 10px; padding: 12px 16px;
  }}
  .card-top {{
    display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
    font-size: 12px; color: var(--text-secondary); margin-bottom: 6px;
  }}
  .badge {{
    font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 999px;
    color: #fff; background: var(--c-light);
  }}
  @media (prefers-color-scheme: dark) {{ .badge {{ background: var(--c-dark); }} }}
  :root[data-theme="dark"] .badge {{ background: var(--c-dark); }}
  .card-date {{ margin-left: auto; color: var(--muted); }}
  .card-title {{
    display: block; font-size: 15px; font-weight: 600; color: var(--text-primary);
    text-decoration: none; margin-bottom: 4px;
  }}
  .card-title:hover {{ text-decoration: underline; }}
  .card-summary {{ font-size: 13px; color: var(--text-secondary); margin: 4px 0; }}
  .card-extra {{ font-size: 12px; color: var(--muted); }}
  .flag {{
    display: inline-block; font-size: 11px; margin-top: 6px; padding: 1px 6px;
    border-radius: 6px; border: 1px solid var(--border); color: var(--text-secondary);
  }}
  footer {{
    max-width: 1100px; margin: 0 auto; padding: 16px 24px 40px;
    color: var(--muted); font-size: 12px;
  }}
  footer a {{ color: var(--text-secondary); }}
</style>
</head>
<body>
<header>
  <h1>3D Printing Intelligence Radar</h1>
  <p class="subtitle">Vygenerováno {esc(generated_at)} · syrový přehled ze zdrojů se stavem READY + CUSTOM adaptéry</p>
  <p class="disclaimer">
    V1 — AI processing stage (relevance/novelty scoring, EVENT deduplikace napříč zdroji, CZ překlad,
    kategorizace do HW/SW/AI/Materials/...) zatím není implementovaná. Tohle je chronologický přehled
    toho, co fetch stage posbírala, seskupený jen podle typu ingestu.
  </p>
</header>
{render_anomaly_banner(anomalies)}
<div class="stats">
  {"".join(stat_tiles)}
</div>
<div id="filter-box">
  <input id="search" type="text" placeholder="Filtrovat podle titulku nebo zdroje…" oninput="filterCards()">
</div>
<main>
{sections}
</main>
<footer>
  <p>Zdrojová data: <a href="data/3dprinting_raw.json">3dprinting_raw.json</a> ·
  generováno skriptem <code>build_site.py</code> nad výstupem <code>fetch_3dprinting.py</code>.</p>
</footer>
<script>
function filterCards() {{
  const q = document.getElementById('search').value.toLowerCase();
  document.querySelectorAll('.card').forEach(card => {{
    const text = card.innerText.toLowerCase();
    card.style.display = text.includes(q) ? '' : 'none';
  }});
  document.querySelectorAll('.group').forEach(group => {{
    const visible = [...group.querySelectorAll('.card')].some(c => c.style.display !== 'none');
    group.style.display = visible ? '' : 'none';
  }});
}}
</script>
</body>
</html>
"""


def main():
    args = parse_args()
    items = load_json(args.input, [])
    state = load_json(args.state, {})
    source_names = load_source_names(args.sources)
    anomalies = build_anomalies(state, source_names)

    generated_at = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "data").mkdir(parents=True, exist_ok=True)

    html_out = build_html(items, anomalies, generated_at)
    (output_dir / "index.html").write_text(html_out, encoding="utf-8")

    input_path = Path(args.input)
    if input_path.exists():
        shutil.copyfile(input_path, output_dir / "data" / "3dprinting_raw.json")

    print(f"Site vygenerován: {output_dir / 'index.html'}")
    print(f"Položek: {len(items)}, anomálií: {len(anomalies)}")


if __name__ == "__main__":
    main()
