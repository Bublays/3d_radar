"""
3D Printing Intelligence Radar — fetch stage (V1)

Čte MASTER_SOURCE_LIST CSV, stáhne obsah ze zdrojů se stavem READY (RSS/Atom)
a z ručně napojených CUSTOM adaptérů (arXiv, GitHub Discovery, Klipper),
znormalizuje záznamy a uloží je do output/3dprinting_raw.json.

Zdroje se stavem WEB / BRIDGE / QUERY / VERIFY / BENCHMARK jsou v1 záměrně
přeskočeny (adaptéry pro ně přijdou v další fázi) — viz README.md.
"""
import argparse
import csv
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlencode

import feedparser
import requests

DEFAULT_SOURCE_FILE = "config/3d_printing_sources.csv"
DEFAULT_OUTPUT_FILE = "output/3dprinting_raw.json"
DEFAULT_STATE_FILE = "state/source_health.json"
REQUEST_TIMEOUT = 45
USER_AGENT = (
    "3D-Printing-Intelligence-Radar/1.0 "
    "(RSS/Atom research aggregator)"
)
# Kolik po sobě jdoucích běhů s 0 položkami u zdroje, který dřív běžně
# vracel obsah, se má nahlásit jako anomálie (podezření na mrtvý endpoint).
ANOMALY_THRESHOLD = 3
# Minimální počet hvězd pro GitHub Discovery – bez prahu propouští
# SEO-spam repozitáře (ověřeno živě 19.8.2026 na dotaz "3d-printing").
GITHUB_MIN_STARS = 5

# ─────────────────────────────────────────────────────────────────────────────
# arXiv — dotazy jako seznamy frází; každá fráze se prefixuje `all:`
# samostatně, aby AND spojoval dvě pole-scoped fráze, ne frázi + volný text.
# ─────────────────────────────────────────────────────────────────────────────
ARXIV_QUERY_TERMS = [
    ['"additive manufacturing"', '"artificial intelligence"'],
    ['"additive manufacturing"', '"machine learning"'],
    ['"additive manufacturing"', '"deep learning"'],
    ['"additive manufacturing"', '"reinforcement learning"'],
    ['"additive manufacturing"', '"digital twin"'],
    ['"additive manufacturing"', '"closed-loop"'],
    ['"additive manufacturing"', '"defect detection"'],
    ['"additive manufacturing"', '"in-situ monitoring"'],
    ['"3D printing"', '"computer vision"'],
    ['"3D printing"', '"artificial intelligence"'],
    ['"3D printing"', '"large language model"'],
    ['"generative design"', "manufacturing"],
    ['"AI CAD"'],
    ['"generative CAD"'],
]

# ─────────────────────────────────────────────────────────────────────────────
# GitHub Discovery
# ─────────────────────────────────────────────────────────────────────────────
GITHUB_DISCOVERY_QUERIES = [
    "3d-printing",
    "additive-manufacturing",
    "slicer 3d-printing",
    "klipper 3d-printing",
    "generative-cad",
    "ai-cad",
    "text-to-3d",
    "image-to-3d",
    "3d-generation",
]

# ─────────────────────────────────────────────────────────────────────────────
# Filtry pro vysokoobjemové/širokozáběrové RSS zdroje (title+summary keyword
# match). Klíč musí přesně odpovídat sloupci "name" v CSV.
# ─────────────────────────────────────────────────────────────────────────────
SOURCE_KEYWORDS = {
    "Hackaday": [
        "3d print",
        "3d-print",
        "additive manufacturing",
        "slicer",
        "klipper",
        "marlin",
        "cad",
        "filament",
        "resin printer",
    ],
    "Siemens Digital Industries Software": [
        "additive manufacturing",
        "3d printing",
        "nx additive",
        "generative design",
        "digital twin",
        "industrial 3d",
    ],
    "Nature Portfolio Search": [
        "additive manufacturing",
        "3d printing",
        "laser powder bed fusion",
        "lpbf",
        "directed energy deposition",
        "generative design",
    ],
}

# Vysvětlení stavů, které skript v1 (zatím) nezpracovává — použito v logu,
# ať je z výstupu jasné PROČ je zdroj přeskočen, ne jen ŽE je přeskočen.
STATUS_MESSAGES = {
    "WEB": "vyžaduje web-scraping/change-detection adaptér (fáze 2).",
    "BRIDGE": "vyžaduje scraping + CN/KO→CZ translation bridge (fáze 2).",
    "QUERY": "vyžaduje custom query/search adaptér (fáze 2).",
    "CUSTOM": "má být napojen na jmenný CUSTOM handler, ale žádný "
              "v main() nenašel shodu podle názvu zdroje – zkontroluj "
              "routing.",
    "VERIFY": "endpoint vyžaduje manuální ověření, než půjde do READY "
              "(viz sloupec notes v CSV).",
    "BENCHMARK": "záměrně vypnutý kontrolní zdroj, nejde o ingest.",
}


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--hours", type=int, default=48, help="Časové okno v hodinách.")
    p.add_argument("--sources", default=DEFAULT_SOURCE_FILE, help="CSV MASTER SOURCE LIST.")
    p.add_argument("--output", default=DEFAULT_OUTPUT_FILE, help="Výstupní JSON.")
    p.add_argument("--state", default=DEFAULT_STATE_FILE, help="Stavový soubor pro health/anomaly tracking.")
    p.add_argument("--priority", default="A+,A,B", help="Povolené priority, např. A+,A,B.")
    return p.parse_args()


# ─────────────────────────────────────────────────────────────────────────────
# Utility
# ─────────────────────────────────────────────────────────────────────────────
def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_bool(value) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "y", "ano")


def clean_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def make_id(source: str, link: str, title: str) -> str:
    raw = f"{source}|{link}|{title}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def parse_date(entry) -> datetime:
    for field in ("published_parsed", "updated_parsed", "created_parsed"):
        value = getattr(entry, field, None)
        if value:
            try:
                return datetime(*value[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return utc_now()


def fetch_bytes(url: str) -> bytes:
    """Stáhne URL s explicitním timeoutem (feedparser sám timeout nehlídá,
    takže bychom bez tohoto kroku mohli na jednom pomalém/mrtvém zdroji
    zaseknout celý běh)."""
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.content


# ─────────────────────────────────────────────────────────────────────────────
# MASTER SOURCE LIST
# ─────────────────────────────────────────────────────────────────────────────
def load_sources(csv_path: str, allowed_priorities: set) -> list:
    path = Path(csv_path)
    if not path.exists():
        print(f"ERR source config nenalezen: {path}", file=sys.stderr)
        return []
    sources = []
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if not row.get("source_id"):
                continue
            if not parse_bool(row.get("enabled", "")):
                continue
            priority = row.get("priority", "").strip()
            if priority not in allowed_priorities:
                continue
            sources.append(row)
    return sources


# ─────────────────────────────────────────────────────────────────────────────
# Relevance pre-filter
# ─────────────────────────────────────────────────────────────────────────────
def is_relevant(title: str, summary: str, source_name: str) -> bool:
    keywords = SOURCE_KEYWORDS.get(source_name, [])
    if not keywords:
        return True
    text = (title + " " + summary).lower()
    return any(keyword.lower() in text for keyword in keywords)


# ─────────────────────────────────────────────────────────────────────────────
# Normalized record
# ─────────────────────────────────────────────────────────────────────────────
def create_record(
    source,
    title: str,
    link: str,
    published: datetime,
    summary: str = "",
    external_id: str = "",
    raw_type: str = "feed",
    extra=None,
):
    record_id = external_id or make_id(source["name"], link, title)
    result = {
        "id": record_id,
        "source_id": source.get("source_id", ""),
        "source": source.get("name", ""),
        "source_class": source.get("source_class", ""),
        "source_category": source.get("category", ""),
        "country": source.get("country", ""),
        "language": source.get("language", ""),
        "priority": source.get("priority", ""),
        "translation_required": parse_bool(source.get("translation_required", "")),
        "ingest_type": source.get("ingest_type", ""),
        "raw_type": raw_type,
        "title": clean_html(title)[:500],
        "link": link,
        "published": published.isoformat(),
        "summary": clean_html(summary)[:4000],
        # tyto hodnoty doplní další AI krok (klasifikace/scoring/dedup)
        "category": None,
        "subcategory": None,
        "tags": [],
        "relevance_score": None,
        "novelty_score": None,
        "event_id": None,
        "processed": False,
    }
    if extra:
        result["extra"] = extra
    return result


# ─────────────────────────────────────────────────────────────────────────────
# RSS / Atom
# ─────────────────────────────────────────────────────────────────────────────
def fetch_feed(source, since: datetime) -> list:
    name = source["name"]
    url = source["feed_or_endpoint"]
    try:
        raw = fetch_bytes(url)
    except Exception as e:
        print(f"  ERR {name}: {e}", file=sys.stderr)
        return []
    feed = feedparser.parse(raw)
    if getattr(feed, "bozo", False):
        print(f"  WARN {name}: {getattr(feed, 'bozo_exception', '')}", file=sys.stderr)
    print(f"  {name}: {len(feed.entries)} entries")
    result = []
    skipped_old = 0
    skipped_filter = 0
    for entry in feed.entries:
        published = parse_date(entry)
        if published < since:
            skipped_old += 1
            continue
        title = entry.get("title", "")
        summary = entry.get("summary") or entry.get("description") or ""
        if not is_relevant(title, summary, name):
            skipped_filter += 1
            continue
        link = entry.get("link", "")
        external_id = entry.get("id") or entry.get("guid") or ""
        result.append(
            create_record(
                source=source,
                title=title,
                link=link,
                published=published,
                summary=summary,
                external_id=external_id,
                raw_type="rss_atom",
            )
        )
    print(f"       accepted={len(result)} old={skipped_old} filtered={skipped_filter}")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# arXiv API
# ─────────────────────────────────────────────────────────────────────────────
def build_arxiv_search_query(terms: list) -> str:
    return " AND ".join(f"all:{term}" for term in terms)


def fetch_arxiv(source, since: datetime) -> list:
    all_items = []
    seen = set()
    for terms in ARXIV_QUERY_TERMS:
        query_label = " AND ".join(terms)
        params = {
            "search_query": build_arxiv_search_query(terms),
            "start": 0,
            "max_results": 30,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        url = "https://export.arxiv.org/api/query?" + urlencode(params)
        try:
            raw = fetch_bytes(url)
        except Exception as e:
            print(f"  ERR arXiv query {query_label}: {e}", file=sys.stderr)
            continue
        feed = feedparser.parse(raw)
        for entry in feed.entries:
            published = parse_date(entry)
            if published < since:
                continue
            link = entry.get("link", "")
            arxiv_id = entry.get("id", link)
            if arxiv_id in seen:
                continue
            seen.add(arxiv_id)
            authors = [a.get("name", "") for a in entry.get("authors", [])]
            all_items.append(
                create_record(
                    source=source,
                    title=entry.get("title", ""),
                    link=link,
                    published=published,
                    summary=entry.get("summary", ""),
                    external_id=arxiv_id,
                    raw_type="arxiv",
                    extra={"authors": authors, "matched_query": query_label},
                )
            )
    print(f"  arXiv: {len(all_items)} unikátních papers")
    return all_items


# ─────────────────────────────────────────────────────────────────────────────
# GitHub Discovery
# ─────────────────────────────────────────────────────────────────────────────
def github_headers() -> dict:
    token = os.getenv("GITHUB_TOKEN", "")
    headers = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_github_discovery(source, since: datetime) -> list:
    headers = github_headers()
    result = []
    seen = set()
    skipped_low_quality = 0
    since_date = since.strftime("%Y-%m-%d")
    for query in GITHUB_DISCOVERY_QUERIES:
        search = f"{query} pushed:>={since_date}"
        params = {"q": search, "sort": "updated", "order": "desc", "per_page": 20}
        try:
            r = requests.get(
                "https://api.github.com/search/repositories",
                params=params,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
            r.raise_for_status()
        except Exception as e:
            print(f"  ERR GitHub discovery {query}: {e}", file=sys.stderr)
            continue
        for repo in r.json().get("items", []):
            repo_id = str(repo.get("id", ""))
            if repo_id in seen:
                continue
            seen.add(repo_id)
            # Ověřeno živě 19.8.2026: bez tohoto filtru "sort=updated"
            # přednostně vrací SEO-spam repozitáře (fork-farmy, "Ultimate
            # Setup 2026" apod.), ne skutečné projekty.
            if repo.get("fork"):
                skipped_low_quality += 1
                continue
            if repo.get("stargazers_count", 0) < GITHUB_MIN_STARS:
                skipped_low_quality += 1
                continue
            pushed_at = repo.get("pushed_at")
            try:
                published = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
            except Exception:
                published = utc_now()
            description = repo.get("description") or ""
            result.append(
                create_record(
                    source=source,
                    title=repo.get("full_name", ""),
                    link=repo.get("html_url", ""),
                    published=published,
                    summary=description,
                    external_id=repo_id,
                    raw_type="github_discovery",
                    extra={
                        "stars": repo.get("stargazers_count", 0),
                        "forks": repo.get("forks_count", 0),
                        "language": repo.get("language"),
                        "topics": repo.get("topics", []),
                        "matched_query": query,
                    },
                )
            )
    print(f"  GitHub Discovery: {len(result)} repositories (odfiltrováno {skipped_low_quality} fork/low-star)")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Klipper změny (Config_Changes.md / Releases.md, ne raw commit stream)
# ─────────────────────────────────────────────────────────────────────────────
def fetch_klipper_changes(source, since: datetime) -> list:
    headers = github_headers()
    paths = ["docs/Config_Changes.md", "docs/Releases.md"]
    result = []
    for path in paths:
        params = {"path": path, "since": since.isoformat(), "per_page": 30}
        url = "https://api.github.com/repos/Klipper3d/klipper/commits"
        try:
            r = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
        except Exception as e:
            print(f"  ERR Klipper {path}: {e}", file=sys.stderr)
            continue
        for commit in r.json():
            sha = commit.get("sha", "")
            commit_info = commit.get("commit", {})
            author = commit_info.get("author", {})
            date_str = author.get("date", "")
            try:
                published = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            except Exception:
                published = utc_now()
            message = commit_info.get("message", "") or ""
            first_line = message.splitlines()[0] if message else "(no commit message)"
            link = commit.get("html_url", "")
            result.append(
                create_record(
                    source=source,
                    title=f"Klipper: {first_line}",
                    link=link,
                    published=published,
                    summary=message,
                    external_id=f"{sha}:{path}",
                    raw_type="github_commit",
                    extra={"repository": "Klipper3d/klipper", "path": path, "sha": sha},
                )
            )
    print(f"  Klipper: {len(result)} relevant commits")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# WEB / BRIDGE / QUERY / VERIFY / BENCHMARK placeholders
# ─────────────────────────────────────────────────────────────────────────────
def unsupported_source(source, status: str):
    reason = STATUS_MESSAGES.get(status, "adapter zatím není implementován.")
    print(f"  SKIP {source['name']}: [{status or '?'}] {reason}")


# ─────────────────────────────────────────────────────────────────────────────
# Deduplikace
# ─────────────────────────────────────────────────────────────────────────────
def deduplicate(items: list) -> list:
    seen = set()
    result = []
    for item in items:
        key = item.get("id") or item.get("link")
        if not key:
            continue
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Health / anomaly tracking
# ─────────────────────────────────────────────────────────────────────────────
def load_state(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(path: str, state: dict):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def update_health(state: dict, source_id: str, source_name: str, count: int, now_iso: str):
    """Vrátí varovný text, pokud zdroj, který dřív běžně vracel obsah,
    vrací 0 položek ANOMALY_THRESHOLD běhů po sobě. Jinak None."""
    if not source_id:
        return None
    entry = state.get(source_id, {"ever_nonzero": False, "consecutive_zero": 0})
    warning = None
    if count > 0:
        entry["ever_nonzero"] = True
        entry["consecutive_zero"] = 0
    else:
        entry["consecutive_zero"] = entry.get("consecutive_zero", 0) + 1
        if entry.get("ever_nonzero") and entry["consecutive_zero"] >= ANOMALY_THRESHOLD:
            warning = (
                f"ANOMALY: {source_name} ({source_id}) vrací 0 položek "
                f"{entry['consecutive_zero']}x po sobě, přestože dřív běžně "
                f"nenulově reportoval — zkontroluj endpoint."
            )
    entry["last_count"] = count
    entry["last_run"] = now_iso
    state[source_id] = entry
    return warning


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
def main():
    args = parse_args()
    allowed_priorities = {p.strip() for p in args.priority.split(",") if p.strip()}
    now = utc_now()
    since = now - timedelta(hours=args.hours)

    print("\n3D Printing Intelligence Radar")
    print("==============================")
    print(f"Window: {args.hours} h")
    print(f"Since:  {since.isoformat()}")
    print(f"Priority: {', '.join(sorted(allowed_priorities))}")

    sources = load_sources(args.sources, allowed_priorities)
    print(f"Sources enabled: {len(sources)}\n")

    state = load_state(args.state)
    warnings = []
    by_source_count = {}

    all_items = []
    for source in sources:
        name = source.get("name", "")
        source_id = source.get("source_id", "")
        status = source.get("endpoint_status", "").strip().upper()
        print(f"── {name} [{status or '?'}] ──")

        items = []
        attempted = True
        # Jmenné CUSTOM handlery mají přednost před obecným READY routingem.
        if name.startswith("arXiv"):
            items = fetch_arxiv(source, since)
        elif name == "GitHub Discovery Radar":
            items = fetch_github_discovery(source, since)
        elif name == "Klipper":
            items = fetch_klipper_changes(source, since)
        elif status == "READY":
            # Routing podle endpoint_status, ne podle ingest_type stringu:
            # READY je jediný stav, kde CSV garantuje, že feed_or_endpoint
            # je opravdu strojově čitelný RSS/Atom feed. Cokoliv jiného
            # (WEB/BRIDGE/QUERY/VERIFY/CUSTOM bez jmenné shody výše) jde do
            # unsupported_source() – i kdyby ingest_type obsahoval "RSS".
            items = fetch_feed(source, since)
        else:
            unsupported_source(source, status)
            attempted = False

        if attempted:
            by_source_count[name] = len(items)
            warning = update_health(state, source_id, name, len(items), now.isoformat())
            if warning:
                warnings.append(warning)

        all_items.extend(items)

    all_items = deduplicate(all_items)
    all_items.sort(key=lambda x: x.get("published", ""), reverse=True)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(all_items, ensure_ascii=False, indent=2), encoding="utf-8")

    save_state(args.state, state)

    print("\n==============================")
    print(f"Celkem: {len(all_items)} unikátních záznamů")
    print(f"Výstup: {output_path}")

    print("\nPo zdrojích (jen zpracované, bez SKIP):")
    for name, count in sorted(by_source_count.items()):
        print(f"  {name:38s} {count}")

    by_type = {}
    for item in all_items:
        raw_type = item.get("raw_type", "unknown")
        by_type[raw_type] = by_type.get(raw_type, 0) + 1
    print("\nPo typu ingestu:")
    for raw_type, count in sorted(by_type.items()):
        print(f"  {raw_type:20s} {count}")

    if warnings:
        print("\n⚠ ANOMALY WATCH:")
        for w in warnings:
            print(f"  {w}")
    else:
        print("\nANOMALY WATCH: bez varování.")


if __name__ == "__main__":
    main()
