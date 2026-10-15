
import json
import os
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
    "OutbreakWatchMVP/0.2 "
    "(public-health news aggregator; contact site administrator)"
)

CURRENT_YEAR = datetime.now(timezone.utc).year
REQUEST_TIMEOUT = 25
MAX_ENTRIES_PER_SOURCE = 100

WHO_AFRO_HOSTS = {"www.afro.who.int", "afro.who.int"}


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

        # Repair WHO report-code URLs when they contain only a report code.
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

    print(f"[INFO] WHO API returned {len(results)} candidate reports")
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
        "%b %d, %Y"
    ):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue

    return ""


def extract_page_date(soup):
    """Extract a publication date from common page metadata."""
    candidates = [
        ("meta", {"property": "article:published_time"}, "content"),
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
            result = parse_date(
                element.get(attr)
                or element.get_text(" ", strip=True)
            )
            if result:
                return result

    for selector in (
        ".date",
        ".field--name-field-publication-date",
        ".published-date",
        "time"
    ):
        element = soup.select_one(selector)
        if element:
            result = parse_date(
                element.get("datetime")
                or element.get_text(" ", strip=True)
            )
            if result:
                return result

    return ""


def extract_web_page(url):
    """
    Extract candidate reports from the WHO Africa updates page.
    Only retain reports whose publication date can be verified
    and falls in the current calendar year.
    """
    response = fetch_page(url)
    soup = BeautifulSoup(response.text, "html.parser")

    results = []
    seen = set()

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

        if canonical == canonical_url(url) or canonical in seen:
            continue

        path = parts.path.lower()
        if path in {"/", "/rss-feeds", "/about-us", "/contact-us"}:
            continue

        seen.add(canonical)

        try:
            report_response = fetch_page(canonical)
            report_soup = BeautifulSoup(
                report_response.text,
                "html.parser"
            )
        except requests.RequestException as exc:
            print(f"[WARN] Could not fetch report {canonical}: {exc}")
            continue

        published = extract_page_date(report_soup)

        if not published:
            print(f"[INFO] Skipping report with unknown date: {canonical}")
            continue

        if int(published[:4]) != CURRENT_YEAR:
            continue

        description_element = (
            report_soup.find("meta", attrs={"name": "description"})
            or report_soup.find(
                "meta",
                attrs={"property": "og:description"}
            )
        )

        description = (
            description_element.get("content", "")
            if description_element
            else ""
        )

        results.append({
            "title": title[:500],
            "link": canonical,
            "description": description[:5000],
            "published": published
        })

        if len(results) >= MAX_ENTRIES_PER_SOURCE:
            break

    print(
        f"[INFO] WHO Africa scraper extracted {len(results)} "
        f"dated reports for {CURRENT_YEAR}"
    )
    return results


def collect_once():
    """Collect, classify, deduplicate, and save articles from enabled sources."""
    print("[INFO] Collector started", flush=True)
    init_db()
    new_count = 0

    sources = load_sources()
    print(f"[INFO] Loaded {len(sources)} configured sources", flush=True)

    for source in sources:
        if not source.get("enabled"):
            continue

        source_name = source.get("name", "Unknown source")
        source_type = source.get("type")
        print(
            f"[INFO] Checking source: {source_name} "
            f"(type={source_type}, enabled={source.get('enabled')})",
            flush=True,
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
                        f"{parsed.bozo_exception}"
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
                    f"{source_type}"
                )
                continue

        except Exception as exc:
            print(f"[WARN] Source failed: {source_name}: {exc}")
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
                        f"{title}"
                    )

                    # Do not guess a date. Retain the WHO API record
                    # with its original date string, if present.
                    if source_type == "web_scrape":
                        continue
                else:
                    published = parsed_published

                    # Scraped WHO Africa pages must be from the current year.
                    if (
                        source_type == "web_scrape"
                        and int(published[:4]) != CURRENT_YEAR
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
                    f"{source_name} ({title}): {exc}"
                )
                continue

        print(
            f"[INFO] Collection finished for {source_name}: "
            f"{source_new_count} new records"
        )

    print(f"[INFO] Total new records collected: {new_count}")
    return new_count


if __name__ == "__main__":
    print(f"Collected {collect_once()} new records.")
