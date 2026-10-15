
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, urljoin

import feedparser
import requests
from bs4 import BeautifulSoup
from rapidfuzz.fuzz import ratio
from ai_processor import classify_and_summarize


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = os.getenv(
    "DATABASE_PATH",
    str(BASE_DIR / "outbreak_watch.db")
)

USER_AGENT = (
    "OutbreakWatchMVP/0.3 "
    "(public-health news aggregator)"
)

CURRENT_YEAR = datetime.now(timezone.utc).year
REQUEST_TIMEOUT = 25
MAX_ENTRIES_PER_SOURCE = 100

WHO_AFRO_HOSTS = {"www.afro.who.int", "afro.who.int"}

DATE_PATTERN = re.compile(
    r"\b(?:"
    r"\d{1,2}\s+"
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|"
    r"May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|"
    r"Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\s+\d{4}"
    r"|"
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|"
    r"May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|"
    r"Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\s+\d{1,2},?\s+\d{4}"
    r"|"
    r"\d{4}-\d{2}-\d{2}"
    r")\b",
    re.IGNORECASE,
)

OUTBREAK_KEYWORDS = (
    "outbreak",
    "ebola",
    "mpox",
    "cholera",
    "yellow fever",
    "measles",
    "polio",
    "marburg",
    "disease outbreak",
    "disease-outbreak",
    "emergency",
    "emergencies",
    "situation report",
    "weekly bulletin",
    "health threat",
    "public health",
    "epidemic",
    "avian influenza",
    "influenza",
)


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_url(url: str) -> str:
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


def fetch_page(url):
    response = requests.get(
        url,
        headers={"User-Agent": USER_AGENT},
        timeout=REQUEST_TIMEOUT
    )
    response.raise_for_status()
    return response


def extract_who_api(url):
    """Collect structured reports from the WHO Disease Outbreak News API."""
    print(f"[INFO] Requesting WHO API: {url}", flush=True)

    response = requests.get(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json"
        },
        timeout=REQUEST_TIMEOUT
    )
    response.raise_for_status()
    payload = response.json()

    if isinstance(payload, dict):
        payload = payload.get(
            "Items",
            payload.get("items", payload.get("value", []))
        )

    if not isinstance(payload, list):
        raise ValueError("WHO API response was not a list of records")

    results = []

    for item in payload:
        if not isinstance(item, dict):
            continue

        title = (
            item.get("OverrideTitle")
            if item.get("UseOverrideTitle")
            else None
        ) or item.get("Title") or "Untitled WHO report"

        link = item.get("ItemDefaultUrl") or ""
        slug = item.get("UrlName") or ""

        if link and not link.startswith(("http://", "https://")):
            link = urljoin("https://www.who.int/", link.lstrip("/"))

        if link:
            parts = urlsplit(link)
            final_segment = parts.path.strip("/").split("/")[-1]

            if (
                parts.netloc.lower() in {"who.int", "www.who.int"}
                and final_segment
                and "-DON" in final_segment
                and "/" not in parts.path.strip("/")
            ):
                link = (
                    "https://www.who.int/emergencies/"
                    "disease-outbreak-news/item/"
                    f"{final_segment}"
                )
        elif slug:
            link = (
                "https://www.who.int/emergencies/"
                f"disease-outbreak-news/item/{slug.strip('/')}"
            )

        published = (
            item.get("PublicationDateAndTime")
            or item.get("PublicationDate")
            or item.get("DateCreated")
            or ""
        )

        description = " ".join(
            str(item.get(key) or "")
            for key in (
                "Summary",
                "Overview",
                "Epidemiology",
                "Assessment"
            )
            if item.get(key)
        )

        if link:
            results.append({
                "title": str(title).strip(),
                "link": link,
                "description": description.strip(),
                "published": str(published)
            })

    print(
        f"[INFO] WHO API returned {len(results)} candidate reports",
        flush=True
    )
    return results


def parse_date(value):
    """Parse a date string into ISO format, or return an empty string."""
    if not value:
        return ""

    value = str(value).strip()

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.date().isoformat()
    except (ValueError, TypeError):
        pass

    for fmt in (
        "%Y-%m-%d",
        "%d %B %Y",
        "%B %d, %Y",
        "%d %b %Y",
        "%b %d, %Y",
        "%B %d %Y",
        "%b %d %Y",
        "%d-%m-%Y",
        "%m/%d/%Y",
        "%d/%m/%Y",
    ):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue

    return ""


def extract_page_date(soup):
    """Extract publication dates from common HTML metadata and date elements."""
    candidates = [
        ("meta", {"property": "article:published_time"}, "content"),
        ("meta", {"property": "article:modified_time"}, "content"),
        ("meta", {"name": "date"}, "content"),
        ("meta", {"name": "pubdate"}, "content"),
        ("meta", {"name": "publish-date"}, "content"),
        ("meta", {"name": "publication_date"}, "content"),
        ("meta", {"name": "DC.date"}, "content"),
        ("meta", {"name": "dcterms.date"}, "content"),
        ("meta", {"itemprop": "datePublished"}, "content"),
        ("meta", {"itemprop": "dateCreated"}, "content"),
        ("time", {"datetime": True}, "datetime"),
    ]

    for tag, attrs, attr in candidates:
        element = soup.find(tag, attrs=attrs)
        if element:
            value = (
                element.get(attr)
                or element.get_text(" ", strip=True)
            )
            result = parse_date(value)
            if result:
                return result

    for selector in (
        ".date",
        ".field--name-field-publication-date",
        ".published-date",
        ".field--name-field-date",
        "time",
    ):
        for element in soup.select(selector):
            value = (
                element.get("datetime")
                or element.get_text(" ", strip=True)
            )
            result = parse_date(value)
            if result:
                return result

    # Some pages store dates in JSON-LD structured metadata.
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue

        objects = data if isinstance(data, list) else [data]

        for obj in objects:
            if not isinstance(obj, dict):
                continue

            for key in ("datePublished", "dateCreated", "dateModified"):
                result = parse_date(obj.get(key))
                if result:
                    return result

    return ""


def extract_listing_date(link_tag):
    """
    Look for a date displayed near a link on the listing page.
    Limit the search to nearby text to reduce unrelated date matches.
    """
    parent = link_tag

    for _ in range(4):
        if parent is None:
            break

        text = " ".join(parent.get_text(" ", strip=True).split())

        # Search only if the nearby text is reasonably short.
        if len(text) <= 1500:
            match = DATE_PATTERN.search(text)
            if match:
                parsed = parse_date(match.group(0))
                if parsed:
                    return parsed

        parent = parent.parent

    return ""


def is_outbreak_related(title, url):
    searchable = f"{title} {url}".lower()
    return any(word in searchable for word in OUTBREAK_KEYWORDS)


def extract_web_page(url):
    """
    Extract current-year, outbreak-related reports from the WHO Africa
    listing page. Prefer dates shown beside listing links, then fall back
    to the linked page's publication metadata.
    """
    print(f"[INFO] Fetching WHO Africa listing: {url}", flush=True)

    response = fetch_page(url)
    soup = BeautifulSoup(response.text, "html.parser")

    results = []
    seen = set()
    candidate_count = 0
    dated_count = 0

    print(
        f"[INFO] WHO Africa listing fetched: HTTP {response.status_code}",
        flush=True
    )

    for link_tag in soup.select("a[href]"):
        title = " ".join(link_tag.get_text(" ", strip=True).split())
        href = urljoin(url, link_tag.get("href", ""))

        if not title or len(title) < 12:
            continue

        parts = urlsplit(href)

        if (
            parts.scheme not in {"http", "https"}
            or parts.netloc.lower() not in WHO_AFRO_HOSTS
        ):
            continue

        canonical = canonical_url(href)

        if canonical == canonical_url(url) or canonical in seen:
            continue

        path = parts.path.lower()

        # Limit candidates to likely news, publications, and outbreak pages.
        if not any(
            section in path
            for section in (
                "/news/",
                "/publication/",
                "/publications/",
                "/health-topics/",
                "/outbreak",
                "/emergencies",
            )
        ):
            continue

        if not is_outbreak_related(title, path):
            continue

        seen.add(canonical)
        candidate_count += 1

        # First try the date displayed next to the listing link.
        published = extract_listing_date(link_tag)
        description = ""

        # Fetch the article when the listing date is missing or when
        # metadata may provide a more reliable description.
        try:
            report_response = fetch_page(canonical)
            report_soup = BeautifulSoup(
                report_response.text,
                "html.parser"
            )

            if not published:
                published = extract_page_date(report_soup)

            description_element = (
                report_soup.find("meta", attrs={"name": "description"})
                or report_soup.find(
                    "meta", attrs={"property": "og:description"}
                )
            )

            if description_element:
                description = (
                    description_element.get("content", "") or ""
                ).strip()

        except requests.RequestException as exc:
            print(
                f"[WARN] Could not fetch report {canonical}: {exc}",
                flush=True
            )

        if not published:
            print(
                f"[INFO] Skipping report with unknown date: {canonical}",
                flush=True
            )
            continue

        if not published.startswith(str(CURRENT_YEAR)):
            continue

        dated_count += 1

        results.append({
            "title": title[:500],
            "link": canonical,
            "description": description[:5000],
            "published": published
        })

        if len(results) >= MAX_ENTRIES_PER_SOURCE:
            break

    print(
        f"[INFO] WHO Africa candidates: {candidate_count}; "
        f"dated current-year reports: {dated_count}; "
        f"reports returned: {len(results)}",
        flush=True
    )

    return results


def collect_once():
    """Collect, classify, deduplicate, and save articles from enabled sources."""
    print("[INFO] Collector started", flush=True)
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
            f"(type={source_type}, enabled={source.get('enabled')})",
            flush=True
        )

        try:
            if source_type == "rss":
                parsed = feedparser.parse(
                    source["url"],
                    request_headers={"User-Agent": USER_AGENT}
                )

                if parsed.bozo and not parsed.entries:
                    print(
                        f"[WARN] Could not read feed: {source_name}: "
                        f"{parsed.bozo_exception}",
                        flush=True
                    )
                    continue

                entries = [{
                    "title": (entry.get("title") or "Untitled").strip(),
                    "link": entry.get("link", ""),
                    "description": (
                        entry.get("summary")
                        or entry.get("description")
                        or ""
                    ),
                    "published": (
                        entry.get("published")
                        or entry.get("updated")
                        or ""
                    )
                } for entry in parsed.entries[:MAX_ENTRIES_PER_SOURCE]]

            elif source_type == "who_api":
                entries = extract_who_api(
                    source["url"]
                )[:MAX_ENTRIES_PER_SOURCE]

            elif source_type == "web_scrape":
                entries = extract_web_page(
                    source["url"]
                )[:MAX_ENTRIES_PER_SOURCE]

            else:
                print(
                    f"[WARN] Unsupported source type for {source_name}: "
                    f"{source_type}",
                    flush=True
                )
                continue

        except Exception as exc:
            print(
                f"[WARN] Source failed: {source_name}: {exc}",
                flush=True
            )
            continue

        source_new_count = 0

        for entry in entries:
            title = (entry.get("title") or "Untitled").strip()
            link = canonical_url(entry.get("link", ""))

            if not title or not link:
                continue

            published = entry.get("published", "")
            description = entry.get("description", "")

            if source_type in {"who_api", "web_scrape"}:
                parsed_published = parse_date(published)

                if not parsed_published:
                    print(
                        f"[WARN] Publication date could not be parsed: "
                        f"{title}",
                        flush=True
                    )

                    # Do not guess a publication date.
                    if source_type == "web_scrape":
                        continue
                else:
                    published = parsed_published

                    # Only the WHO Africa scraper is restricted to the
                    # current year. The WHO API may populate older records.
                    if (
                        source_type == "web_scrape"
                        and not published.startswith(str(CURRENT_YEAR))
                    ):
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
                        if ratio(title.lower(), old["title"].lower()) >= 94:
                            duplicate_id = old["id"]
                            break

                    print(
                        f"[INFO] Processing article with AI: {title[:120]}",
                        flush=True
                    )

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
                    f"[WARN] Could not save article from "
                    f"{source_name} ({title}): {exc}",
                    flush=True
                )
                continue

        print(
            f"[INFO] Collection finished for {source_name}: "
            f"{source_new_count} new records",
            flush=True
        )

    print(
        f"[INFO] Total new records collected: {new_count}",
        flush=True
    )
    return new_count


if __name__ == "__main__":
    print(f"Collected {collect_once()} new records.", flush=True)
