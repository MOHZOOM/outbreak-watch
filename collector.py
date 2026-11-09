import json
import os
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, urljoin
from urllib.robotparser import RobotFileParser
from typing import List, Optional

import requests
from bs4 import BeautifulSoup
from rapidfuzz.fuzz import ratio
from ai_processor import classify_and_summarize


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = os.getenv(
    "DATABASE_PATH",
    str(BASE_DIR / "outbreak_watch.db")
)

CURRENT_YEAR = 2026
REQUEST_TIMEOUT = 25
MAX_ENTRIES_PER_SOURCE = 100
# Total time allowed for opening one AI-scraped source's articles. Some servers
# trickle data so slowly that the per-read timeout never fires.
AI_SOURCE_DEADLINE_SECONDS = 90

USER_AGENT = (
    "OutbreakWatchMVP/0.4 "
    "(public-health news aggregator)"
)

OUTBREAK_KEYWORDS = (
    "outbreak", "ebola", "mpox", "cholera", "yellow fever",
    "measles", "polio", "marburg", "epidemic", "influenza",
    "disease", "emergency", "public health", "health threat",
    "meningitis", "dengue", "plague", "situation report",
    "health emergency", "avian influenza", "hepatitis"
)


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_url(url):
    parts = urlsplit((url or "").strip())
    return urlunsplit((
        parts.scheme.lower(),
        parts.netloc.lower(),
        parts.path.rstrip("/"),
        "",
        ""
    ))


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(
            (BASE_DIR / "schema.sql").read_text(encoding="utf-8")
        )


def load_sources():
    return json.loads(
        (BASE_DIR / "sources.json").read_text(encoding="utf-8")
    )


_robots_cache = {}


def allowed_by_robots(url):
    """Return True if the site's robots.txt allows us to fetch this URL."""
    parts = urlsplit(url)
    base = f"{parts.scheme}://{parts.netloc}"

    parser = _robots_cache.get(base)
    if parser is None:
        parser = RobotFileParser()
        try:
            r = requests.get(
                base + "/robots.txt",
                headers={"User-Agent": USER_AGENT},
                timeout=REQUEST_TIMEOUT,
            )
            parser.parse(r.text.splitlines() if r.status_code == 200 else [])
        except requests.RequestException:
            parser.parse([])
        _robots_cache[base] = parser

    return parser.can_fetch("OutbreakWatchMVP", url)


def inspect_article(url):
    """Open an article once and return (final URL, publication date, description).

    Returns empty values when the page is missing or broken.
    """
    if not url or urlsplit(url).scheme not in {"http", "https"}:
        return "", "", ""

    try:
        response = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        if response.status_code >= 400:
            print(
                f"[WARN] Source URL returned HTTP {response.status_code}: {url}",
                flush=True,
            )
            return "", "", ""

        soup = BeautifulSoup(response.text, "html.parser")

        # Some sites display a branded not-found page with HTTP 200.
        page_text = soup.get_text(" ", strip=True).lower()
        not_found_phrases = (
            "this page cannot be found",
            "the page or file you are trying to access cannot be found",
            "the requested url was not found on the server",
        )
        if any(phrase in page_text for phrase in not_found_phrases):
            print(
                f"[WARN] Source returned a not-found page despite HTTP {response.status_code}: {url}",
                flush=True,
            )
            return "", "", ""

        final_url = response.url
        if urlsplit(final_url).scheme not in {"http", "https"}:
            return "", "", ""

        tag = (
            soup.find("meta", attrs={"name": "description"})
            or soup.find("meta", attrs={"property": "og:description"})
        )
        description = tag.get("content", "") if tag else ""

        # When available, cross-check the date shown on the actual article page.
        return final_url, extract_page_date(soup), description
    except requests.RequestException as exc:
        print(f"[WARN] Could not verify source URL {url}: {exc}", flush=True)
        return "", "", ""


def parse_date(value):
    """Parse supported date strings and return YYYY-MM-DD."""
    if not value:
        return ""

    value = str(value).strip()

    try:
        return datetime.fromisoformat(
            value.replace("Z", "+00:00")
        ).date().isoformat()
    except (ValueError, TypeError):
        pass

    formats = (
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%d %B %Y",
        "%B %d, %Y",
        "%d %b %Y",
        "%b %d, %Y",
        "%d/%m/%Y",
        "%m/%d/%Y",
        "%d-%m-%Y",
        "%d-%b-%Y",
        "%d-%B-%Y"
    )

    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue

    # Month names, full or abbreviated ("October", "Oct", "Sept.").
    month = (
        r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
        r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|"
        r"Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?"
    )

    patterns = (
        r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b",
        r"\b(\d{1,2})\s+" + month + r"\s+(20\d{2})\b",
        r"\b" + month + r"\s+(\d{1,2}),?\s+(20\d{2})\b",
    )

    for pattern in patterns:
        match = re.search(pattern, value, re.IGNORECASE)
        if not match:
            continue

        try:
            matched = match.group(0).replace(",", "").replace(".", "")
            matched = re.sub(r"\bSept\b", "Sep", matched, flags=re.IGNORECASE)
            if re.match(r"20\d{2}[-/]", matched):
                matched = matched.replace("/", "-")
                parts = matched.split("-")
                return datetime(
                    int(parts[0]), int(parts[1]), int(parts[2])
                ).date().isoformat()

            return parse_date_simple(matched)
        except (ValueError, TypeError):
            continue

    return ""


def parse_date_simple(value):
    for fmt in (
        "%d %B %Y", "%d %b %Y",
        "%B %d %Y", "%b %d %Y"
    ):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def is_2026_report(value):
    parsed = parse_date(value)
    return bool(parsed and parsed.startswith(f"{CURRENT_YEAR}-"))


def extract_page_date(soup):
    """Extract an article's original publication date from HTML metadata."""
    candidates = (
        ("meta", {"property": "article:published_time"}, "content"),
        ("meta", {"name": "datePublished"}, "content"),
        ("meta", {"name": "date"}, "content"),
        ("meta", {"name": "pubdate"}, "content"),
        ("meta", {"name": "publish-date"}, "content"),
        ("meta", {"name": "publication_date"}, "content"),
        ("meta", {"itemprop": "datePublished"}, "content"),
        ("meta", {"property": "og:article:published_time"}, "content"),
        ("meta", {"name": "DC.date.issued"}, "content"),
        ("meta", {"name": "dcterms.issued"}, "content"),
        ("time", {"datetime": True}, "datetime"),
    )

    for tag, attrs, attr in candidates:
        for element in soup.find_all(tag, attrs=attrs):
            parsed = parse_date(
                element.get(attr) or element.get_text(" ", strip=True)
            )
            if parsed:
                return parsed

    # Inspect structured data, including nested @graph objects.
    for script in soup.find_all(
        "script", attrs={"type": "application/ld+json"}
    ):
        try:
            data = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue

        pending = data if isinstance(data, list) else [data]

        while pending:
            obj = pending.pop(0)

            if isinstance(obj, list):
                pending.extend(obj)
                continue

            if not isinstance(obj, dict):
                continue

            graph = obj.get("@graph")
            if isinstance(graph, list):
                pending.extend(graph)

            for key in ("datePublished", "dateCreated"):
                parsed = parse_date(obj.get(key))
                if parsed:
                    return parsed

    # Check common visible date components.
    for selector in (
        "time[datetime]",
        ".date",
        ".published-date",
        ".publication-date",
        ".field--name-field-publication-date",
        ".article-date",
        ".news-date",
        "[itemprop='datePublished']",
    ):
        for element in soup.select(selector):
            parsed = parse_date(
                element.get("datetime")
                or element.get("content")
                or element.get_text(" ", strip=True)
            )
            if parsed:
                return parsed

    return ""


def extract_listing_date(link_tag):
    """Look for a publication date near a link on a listing page."""
    current = link_tag

    for _ in range(5):
        if current is None:
            break

        text = current.get_text(" ", strip=True)[:2000]
        parsed = parse_date(text)

        if parsed:
            return parsed

        current = current.parent

    return ""


def _find_item_list(data):
    """Find the first list of dicts inside whatever shape the LLM returned."""
    if isinstance(data, list):
        if data and all(isinstance(x, dict) for x in data):
            return data
        for x in data:
            found = _find_item_list(x)
            if found:
                return found
    elif isinstance(data, dict):
        for value in data.values():
            found = _find_item_list(value)
            if found:
                return found
    return []


def _make_article_schema():
    """Pydantic schema that forces ScrapeGraphAI to return a list of articles."""
    from pydantic import BaseModel

    class Article(BaseModel):
        title: str
        url: str
        published_date: Optional[str] = None

    class Articles(BaseModel):
        articles: List[Article]

    return Articles


def _links_from_page(text, base_url):
    """Fallback: pull article-like links out of the page ScrapeGraphAI fetched.

    Returns (items, listing dates by title). The fetched page may be HTML or
    markdown depending on the ScrapeGraphAI version.
    """
    items, dates = [], {}
    image_ends = (".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp")

    if "<a" in text:
        for tag in BeautifulSoup(text, "html.parser").select("a[href]"):
            title = " ".join(tag.get_text(" ", strip=True).split())
            href = tag.get("href", "")
            if len(title) < 25 or href.startswith("#"):
                continue
            if urlsplit(href).path.lower().endswith(image_ends):
                continue
            items.append({"title": title, "url": href, "published_date": None})
            dates.setdefault(title, extract_listing_date(tag))
        return items, dates

    for title, href in re.findall(r"\[([^\]\n]{25,250})\]\(([^)\s]+)\)", text):
        title = " ".join(title.split())
        if "![" in title or title.startswith("!"):
            continue  # image markup, not an article title
        if urlsplit(href).path.lower().endswith(image_ends):
            continue
        items.append({"title": title, "url": href, "published_date": None})
    return items, dates


def _date_near_title(text, title):
    """Look for a visible date in the page text just around a title."""
    i = text.find(title)
    if i < 0:
        return ""
    window = BeautifulSoup(
        text[max(0, i - 300): i + len(title) + 300], "html.parser"
    ).get_text(" ", strip=True)
    return parse_date(window)


def _check_ai_item(item, listing_url, source_host, text, listing_dates):
    """Verify one item the LLM returned. Returns (result or None, skip reason)."""
    title = " ".join(str(item.get("title") or "").split())
    link = canonical_url(urljoin(listing_url, str(item.get("url") or "")))
    host = (urlsplit(link).hostname or "").lower().removeprefix("www.")

    if not title or not link or link == canonical_url(listing_url):
        return None, "no_link"
    # Skip section links such as "/en" or "/news"; articles sit deeper.
    if len([part for part in urlsplit(link).path.split("/") if part]) < 2:
        return None, "no_link"
    if host != source_host:
        return None, "other_host"
    if not allowed_by_robots(link):
        return None, "robots"

    # Never trust the LLM's link or date: verify on the real page.
    verified_link, page_date, description = inspect_article(link)
    if not verified_link:
        return None, "not_verified"

    # Date priority: the article page itself, then the LLM, then the listing.
    published = (
        page_date
        or parse_date(item.get("published_date"))
        or listing_dates.get(title, "")
        or (_date_near_title(text, title) if text else "")
    )
    if not published:
        return None, "no_date"
    if not is_2026_report(published):
        return None, "not_2026"

    relevant_text = (
        title + " " + urlsplit(verified_link).path + " " + description[:1500]
    ).lower()
    if not any(word in relevant_text for word in OUTBREAK_KEYWORDS):
        return None, "not_relevant"

    return {
        "title": title[:500],
        "link": canonical_url(verified_link),
        "description": description[:5000],
        "published": parse_date(published),
    }, ""


def extract_ai_scrape(source):
    """Use ScrapeGraphAI to read a listing page, then verify every link."""
    url = source["url"]
    if not allowed_by_robots(url):
        raise requests.RequestException(f"Blocked by robots.txt: {url}")

    # Imported here so the app still starts if the library is missing.
    from scrapegraphai.graphs import SmartScraperGraph

    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    if "/" not in model:
        model = f"openai/{model}"

    graph = SmartScraperGraph(
        prompt=(
            f"This is a listing page from {source.get('name', 'a public health agency')}. "
            "Return every link whose text is the title of a news item, "
            "outbreak update, travel notice, bulletin, report or article "
            "(titles are usually full sentences). Skip navigation menus, "
            "topic lists, filters, language links and footer links. For each, "
            "give the title, the link exactly as shown, and the published "
            "date in YYYY-MM-DD if it is shown, otherwise null. Include items "
            "even when the date is missing."
        ),
        source=url,
        schema=_make_article_schema(),
        config={
            "llm": {
                "api_key": os.getenv("OPENAI_API_KEY"),
                "model": model,
            },
            "verbose": False,
            "headless": True,
            # Optional per-source browser options from sources.json, e.g.
            # {"load_state": "networkidle"} for pages that load items with JS.
            "loader_kwargs": source.get("loader_kwargs") or {},
        },
    )

    raw = graph.run()
    print(
        f"[DEBUG] ScrapeGraphAI raw result: {str(raw)[:500]}",
        flush=True,
    )

    # The page ScrapeGraphAI's browser fetched, for the fallback and dates.
    try:
        fetched = graph.get_state("doc")
        text = fetched[0].page_content if fetched else ""
    except Exception as exc:
        text = ""
        print(f"[DEBUG] Could not read fetched page: {exc}", flush=True)

    items = _find_item_list(raw)
    fallback_items, listing_dates = _links_from_page(text, url) if text else ([], {})
    if not items and fallback_items:
        items = fallback_items
        print(
            f"[INFO] ScrapeGraphAI's AI step returned no items; using "
            f"{len(items)} links parsed from the page it fetched",
            flush=True,
        )

    source_host = (urlsplit(url).hostname or "").lower().removeprefix("www.")

    # Drop duplicate links before opening any pages.
    unique, seen = [], set()
    for item in items:
        key = canonical_url(urljoin(url, str(item.get("url") or "")))
        if key not in seen:
            seen.add(key)
            unique.append(item)

    # Open the articles in parallel, but stop waiting for stalled servers.
    pool = ThreadPoolExecutor(max_workers=4)
    futures = [
        pool.submit(_check_ai_item, item, url, source_host, text, listing_dates)
        for item in unique[:MAX_ENTRIES_PER_SOURCE]
    ]
    done, not_done = wait(futures, timeout=AI_SOURCE_DEADLINE_SECONDS)
    pool.shutdown(wait=False, cancel_futures=True)

    results, stats = [], {}
    if not_done:
        stats["timed_out"] = len(not_done)
    for future in done:
        try:
            result, reason = future.result()
        except Exception:
            result, reason = None, "error"
        if result:
            results.append(result)
        else:
            stats[reason] = stats.get(reason, 0) + 1

    print(
        f"[INFO] ScrapeGraphAI found {len(items)} items; "
        f"{len(results)} verified 2026 reports from {source_host}; "
        f"skipped: {stats}",
        flush=True,
    )
    return results


def collect_once():
    """Collect only reports published in 2026."""
    print(
        f"[INFO] Collector started; year filter: {CURRENT_YEAR}",
        flush=True
    )

    init_db()

    new_count = 0
    sources = load_sources()

    print(
        f"[INFO] Loaded {len(sources)} configured sources",
        flush=True
    )

    for source in sources:
        if not source.get("enabled"):
            continue

        source_name = source.get("name", "Unknown source")
        source_type = source.get("type")

        print(
            f"[INFO] Checking source: {source_name} "
            f"(type={source_type})",
            flush=True
        )

        try:
            if source_type == "ai_scrape":
                # Every source goes through ScrapeGraphAI (LLM-based), which
                # reads the listing page; every link and date it returns is
                # then verified on the real article page.
                entries = extract_ai_scrape(source)
            else:
                print(
                    f"[WARN] Unsupported source type: {source_type}. "
                    "Only 'ai_scrape' (ScrapeGraphAI) sources are supported.",
                    flush=True
                )
                continue

        except Exception as exc:
            print(
                f"[WARN] Source failed ({source_name}): {exc}",
                flush=True
            )
            continue

        source_new_count = 0

        for entry in entries:
            title = (entry.get("title") or "Untitled").strip()
            link = canonical_url(entry.get("link", ""))
            published = parse_date(entry.get("published", ""))
            description = entry.get("description", "")

            # Final safeguard before AI processing and database insertion.
            if not title or not link or not is_2026_report(published):
                continue

            try:
                with sqlite3.connect(DB_PATH) as conn:
                    conn.row_factory = sqlite3.Row

                    existing = conn.execute(
                        "SELECT id FROM articles WHERE original_url = ?",
                        (link,)
                    ).fetchone()

                    if existing:
                        continue

                    possible = conn.execute(
                        """
                        SELECT id, title
                        FROM articles
                        WHERE collected_at >= datetime('now', '-30 days')
                        """
                    ).fetchall()

                    duplicate_id = None

                    for old in possible:
                        if ratio(
                            title.lower(), old["title"].lower()
                        ) >= 94:
                            duplicate_id = old["id"]
                            break

                    fields = classify_and_summarize(
                        title,
                        description,
                        source_name,
                        published
                    )

                    conn.execute(
                        """
                        INSERT OR IGNORE INTO articles
                        (
                            title, original_url, source_name, published_at,
                            collected_at, description, is_relevant, category,
                            disease_event, country_region, event_date,
                            outbreak_status, ai_summary, ai_processed,
                            duplicate_of
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            title,
                            link,
                            source_name,
                            published,
                            utc_now(),
                            description[:10000],
                            int(fields["is_relevant"]),
                            fields["category"],
                            fields["disease_event"],
                            fields["country_region"],
                            fields["event_date"],
                            fields["outbreak_status"],
                            fields["summary"],
                            int(fields.get("_ai_processed", False)),
                            duplicate_id
                        )
                    )

                    if conn.execute("SELECT changes()").fetchone()[0]:
                        new_count += 1
                        source_new_count += 1

            except Exception as exc:
                print(
                    f"[WARN] Could not save '{title}': {exc}",
                    flush=True
                )

        print(
            f"[INFO] Finished {source_name}: "
            f"{source_new_count} new 2026 records",
            flush=True
        )

    print(
        f"[INFO] Collection finished. New 2026 records: {new_count}",
        flush=True
    )

    return new_count


if __name__ == "__main__":
    print(f"Collected {collect_once()} new records.")
