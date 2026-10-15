
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

CURRENT_YEAR = 2026
REQUEST_TIMEOUT = 25
MAX_ENTRIES_PER_SOURCE = 100

USER_AGENT = (
    "OutbreakWatchMVP/0.3 "
    "(public-health news aggregator)"
)

WHO_AFRO_HOSTS = {
    "www.afro.who.int",
    "afro.who.int"
}

OUTBREAK_KEYWORDS = (
    "outbreak", "ebola", "mpox", "cholera", "yellow fever",
    "measles", "polio", "marburg", "epidemic", "influenza",
    "disease", "emergency", "public health", "health threat",
    "avian influenza", "meningitis", "dengue", "plague",
    "situation report", "health emergency"
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


def fetch_page(url):
    response = requests.get(
        url,
        headers={"User-Agent": USER_AGENT},
        timeout=REQUEST_TIMEOUT
    )
    response.raise_for_status()
    return response


def parse_date(value):
    """Return a parsed date as YYYY-MM-DD, or an empty string."""
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
        "%d %B %Y",
        "%B %d, %Y",
        "%d %b %Y",
        "%b %d, %Y",
        "%Y/%m/%d",
        "%d/%m/%Y",
        "%m/%d/%Y"
    )

    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue

    match = re.search(
        r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b",
        value
    )

    if match:
        try:
            year, month, day = map(int, match.groups())
            return datetime(year, month, day).date().isoformat()
        except ValueError:
            pass

    return ""


def is_2026_report(date_value):
    """Accept only reports with a verified publication date in 2026."""
    parsed = parse_date(date_value)
    return bool(parsed and parsed.startswith("2026-"))


def extract_who_api(url):
    """Extract reports from the WHO Disease Outbreak News API."""
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
        title = (
            item.get("OverrideTitle")
            if item.get("UseOverrideTitle")
            else None
        ) or item.get("Title") or "Untitled WHO report"

        link = item.get("ItemDefaultUrl") or ""
        slug = item.get("UrlName") or ""

        if link and not link.startswith("http"):
            link = urljoin("https://www.who.int/", link.lstrip("/"))

        if link:
            parts = urlsplit(link)
            segment = parts.path.strip("/").split("/")[-1]

            if (
                parts.netloc.lower() in {"who.int", "www.who.int"}
                and "-DON" in segment
                and "/" not in parts.path.strip("/")
            ):
                link = (
                    "https://www.who.int/emergencies/"
                    f"disease-outbreak-news/item/{segment}"
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

        # Filter BEFORE AI processing and database insertion.
        if not is_2026_report(published):
            continue

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
                "published": parse_date(published)
            })

    print(
        f"[INFO] WHO API: {len(results)} reports dated {CURRENT_YEAR}",
        flush=True
    )
    return results


def extract_page_date(soup):
    """Read a publication date from common metadata and page elements."""
    candidates = [
        ("meta", {"property": "article:published_time"}, "content"),
        ("meta", {"property": "article:modified_time"}, "content"),
        ("meta", {"name": "date"}, "content"),
        ("meta", {"name": "pubdate"}, "content"),
        ("meta", {"name": "publish-date"}, "content"),
        ("meta", {"name": "publication_date"}, "content"),
        ("meta", {"itemprop": "datePublished"}, "content"),
        ("time", {"datetime": True}, "datetime")
    ]

    for tag, attrs, attr in candidates:
        element = soup.find(tag, attrs=attrs)

        if element:
            value = element.get(attr) or element.get_text(" ", strip=True)
            parsed = parse_date(value)

            if parsed:
                return parsed

    # Check JSON-LD structured data.
    for script in soup.find_all(
        "script",
        attrs={"type": "application/ld+json"}
    ):
        try:
            data = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue

        objects = data if isinstance(data, list) else [data]

        for obj in objects:
            if not isinstance(obj, dict):
                continue

            for key in ("datePublished", "dateCreated"):
                parsed = parse_date(obj.get(key))
                if parsed:
                    return parsed

    for selector in (
        ".date",
        ".field--name-field-publication-date",
        ".published-date",
        "time"
    ):
        element = soup.select_one(selector)

        if element:
            parsed = parse_date(
                element.get("datetime")
                or element.get_text(" ", strip=True)
            )

            if parsed:
                return parsed

    return ""


def extract_listing_date(link_tag):
    """Try to find a date displayed near an article link."""
    current = link_tag

    for _ in range(4):
        if current is None:
            break

        text = current.get_text(" ", strip=True)[:1500]
        parsed = parse_date(text)

        if parsed:
            return parsed

        current = current.parent

    return ""


def extract_web_page(url):
    """Collect only dated 2026 outbreak reports from WHO Africa."""
    response = fetch_page(url)
    soup = BeautifulSoup(response.text, "html.parser")

    results = []
    seen = set()
    candidate_count = 0

    for link_tag in soup.select("a[href]"):
        title = " ".join(link_tag.get_text(" ", strip=True).split())
        href = urljoin(url, link_tag.get("href", ""))

        if not title or len(title) < 12:
            continue

        parts = urlsplit(href)

        if parts.scheme not in {"http", "https"}:
            continue

        if parts.netloc.lower() not in WHO_AFRO_HOSTS:
            continue

        canonical = canonical_url(href)

        if canonical in seen or canonical == canonical_url(url):
            continue

        path = parts.path.lower()

        if path in {
            "/", "/rss-feeds", "/about-us", "/contact-us"
        }:
            continue

        # Avoid visiting the same link repeatedly.
        seen.add(canonical)

        # Restrict candidates to likely article/report links.
        if not any(word in (title + " " + path).lower()
                   for word in OUTBREAK_KEYWORDS):
            continue

        candidate_count += 1

        # First try the date shown on the listing page.
        published = extract_listing_date(link_tag)

        # Fetch the article if the listing did not provide a usable date.
        description = ""

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
                    "meta",
                    attrs={"property": "og:description"}
                )
            )

            if description_element:
                description = description_element.get("content", "")

        except requests.RequestException as exc:
            print(
                f"[WARN] Could not fetch report {canonical}: {exc}",
                flush=True
            )
            continue

        # Strict 2026-only rule. Unknown dates are excluded.
        if not is_2026_report(published):
            continue

        results.append({
            "title": title[:500],
            "link": canonical,
            "description": description[:5000],
            "published": parse_date(published)
        })

        if len(results) >= MAX_ENTRIES_PER_SOURCE:
            break

    print(
        f"[INFO] WHO Africa candidates: {candidate_count}; "
        f"dated 2026 reports: {len(results)}",
        flush=True
    )

    return results


def collect_once():
    """Collect, filter, classify, deduplicate, and save 2026 reports."""
    print("[INFO] Collector started; year filter: 2026", flush=True)

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
            if source_type == "who_api":
                entries = extract_who_api(source["url"])

            elif source_type == "web_scrape":
                entries = extract_web_page(source["url"])

            elif source_type == "rss":
                parsed_feed = feedparser.parse(
                    source["url"],
                    request_headers={"User-Agent": USER_AGENT}
                )

                entries = []

                for entry in parsed_feed.entries[:MAX_ENTRIES_PER_SOURCE]:
                    published_raw = (
                        entry.get("published")
                        or entry.get("updated")
                        or ""
                    )

                    if not is_2026_report(published_raw):
                        continue

                    entries.append({
                        "title": (entry.get("title") or "Untitled").strip(),
                        "link": entry.get("link", ""),
                        "description": (
                            entry.get("summary")
                            or entry.get("description")
                            or ""
                        ),
                        "published": parse_date(published_raw)
                    })

            else:
                print(
                    f"[WARN] Unsupported source type: {source_type}",
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

            # Final safeguard: no report reaches AI or the database
            # unless its publication date is verified as being in 2026.
            if not is_2026_report(published):
                print(
                    f"[INFO] Excluded non-2026/undated report: {title}",
                    flush=True
                )
                continue

            if not title or not link:
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

                    # Only current-year reports are sent to the AI.
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
                    f"[WARN] Could not save article '{title}': {exc}",
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
