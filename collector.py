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


def fetch_page(url):
    response = requests.get(
        url,
        headers={"User-Agent": USER_AGENT},
        timeout=REQUEST_TIMEOUT
    )
    response.raise_for_status()
    return response


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

    patterns = (
        r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b",
        r"\b(\d{1,2})\s+"
        r"(January|February|March|April|May|June|July|August|"
        r"September|October|November|December)\s+(20\d{2})\b",
        r"\b(January|February|March|April|May|June|July|August|"
        r"September|October|November|December)\s+"
        r"(\d{1,2}),?\s+(20\d{2})\b"
    )

    for pattern in patterns:
        match = re.search(pattern, value, re.IGNORECASE)
        if not match:
            continue

        try:
            matched = match.group(0).replace(",", "")
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


def extract_who_api(url):
    """Read WHO API records and retain only verified 2026 reports."""
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
    rejected_dates = 0

    for item in payload:
        title = (
            item.get("OverrideTitle")
            if item.get("UseOverrideTitle")
            else None
        ) or item.get("Title") or "Untitled WHO report"

        link = (
            item.get("ItemDefaultUrl")
            or item.get("CanonicalUrl")
            or item.get("Url")
            or item.get("URL")
            or ""
        )
        slug = str(item.get("UrlName") or "").strip("/")

        # Temporary diagnostic: inspect WHO URL fields for MERS reports.
        # Match multiple fields because WHO may use an override title or a short title.
        mers_search_text = " ".join(
            str(item.get(field) or "")
            for field in (
                "Title",
                "OverrideTitle",
                "UrlName",
                "ItemDefaultUrl",
                "CanonicalUrl",
                "Url",
                "URL",
            )
        ).lower()

        if (
            "mers" in mers_search_text
            or "middle east respiratory" in mers_search_text
        ):
            print(
                "[DEBUG] MERS URL fields: "
                + json.dumps({
                    "ItemDefaultUrl": item.get("ItemDefaultUrl"),
                    "CanonicalUrl": item.get("CanonicalUrl"),
                    "Url": item.get("Url"),
                    "URL": item.get("URL"),
                    "UrlName": item.get("UrlName"),
                    "Title": item.get("Title"),
                    "OverrideTitle": item.get("OverrideTitle"),
                    "ResolvedTitle": title,
                }, ensure_ascii=False),
                flush=True
            )

        # Prefer WHO's standard DON page when the API provides a DON identifier.
        # This avoids keeping a title-based URL when a canonical DON ID exists.
        if slug and "-DON" in slug.upper():
            link = (
                "https://www.who.int/emergencies/"
                f"disease-outbreak-news/item/{slug}"
            )
        elif link:
            # Preserve the URL provided by WHO; only make relative URLs absolute.
            if not link.startswith(("http://", "https://")):
                link = urljoin("https://www.who.int/", link.lstrip("/"))
        elif slug:
            # Last-resort fallback if WHO supplies a slug but no URL.
            link = urljoin(
                "https://www.who.int/",
                f"emergencies/disease-outbreak-news/item/{slug}"
            )

        if (
            "mers" in mers_search_text
            or "middle east respiratory" in mers_search_text
        ):
            print(
                f"[DEBUG] MERS resolved URL: {link!r}",
                flush=True
            )

        # Prefer original publication dates over modification dates.
        date_fields = (
            "PublicationDateAndTime",
            "PublicationDate",
            "DatePublished",
            "DateCreated",
            "Created"
        )

        raw_date = ""
        parsed_date = ""

        for field in date_fields:
            candidate = item.get(field)
            if candidate:
                parsed_candidate = parse_date(candidate)
                if parsed_candidate:
                    raw_date = str(candidate)
                    parsed_date = parsed_candidate
                    break

        if len(results) < 5 and rejected_dates < 5:
            print(
                f"[DEBUG] WHO title={title!r}; "
                f"date field={raw_date!r}; "
                f"parsed date={parsed_date!r}",
                flush=True
            )

        if not is_2026_report(parsed_date):
            rejected_dates += 1
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
                "published": parsed_date
            })

    print(
        f"[INFO] WHO API: {len(results)} dated 2026 reports; "
        f"{rejected_dates} rejected as undated or outside 2026",
        flush=True
    )

    return results


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


def extract_web_page(url):
    """
    Scrape article pages from the configured website.
    Only verified 2026 publication dates are accepted.
    """
    response = fetch_page(url)
    soup = BeautifulSoup(response.text, "html.parser")

    source_host = (urlsplit(url).hostname or "").lower()
    allowed_hosts = {source_host}

    if source_host.startswith("www."):
        allowed_hosts.add(source_host[4:])
    else:
        allowed_hosts.add("www." + source_host)

    results = []
    seen = set()
    checked = 0
    max_pages_to_check = MAX_ENTRIES_PER_SOURCE * 3

    article_path_hints = (
        "news", "outbreak", "emergency", "emergencies", "update",
        "updates", "publication", "disease", "health", "situation",
    )

    ignored_path_parts = (
        "/rss", "/feed", "/search", "/taxonomy/", "/user/",
        "/contact", "/about", "/privacy", "/accessibility",
    )

    for link_tag in soup.select("a[href]"):
        title = " ".join(link_tag.get_text(" ", strip=True).split())
        href = urljoin(url, link_tag.get("href", ""))
        parts = urlsplit(href)
        host = (parts.hostname or "").lower()
        path = parts.path.lower()

        if not title or len(title) < 12:
            continue

        if parts.scheme not in {"http", "https"}:
            continue

        if host not in allowed_hosts:
            continue

        if any(piece in path for piece in ignored_path_parts):
            continue

        canonical = canonical_url(href)

        if canonical == canonical_url(url) or canonical in seen:
            continue

        seen.add(canonical)

        title_and_path = (title + " " + path).lower()

        keyword_match = any(
            word in title_and_path for word in OUTBREAK_KEYWORDS
        )

        path_hint_match = any(
            word in path for word in article_path_hints
        )

        # Avoid fetching ordinary navigation links.
        if not keyword_match and not path_hint_match:
            continue

        checked += 1

        if checked > max_pages_to_check:
            print(
                "[INFO] Web scraper reached its page-check limit",
                flush=True
            )
            break

        published = extract_listing_date(link_tag)
        description = ""
        article_title = title

        try:
            article_response = fetch_page(canonical)
            article_soup = BeautifulSoup(
                article_response.text,
                "html.parser"
            )

            if not published:
                published = extract_page_date(article_soup)

            # Prefer the actual article heading.
            heading = article_soup.find(["h1", "h2"])

            if heading:
                heading_text = " ".join(
                    heading.get_text(" ", strip=True).split()
                )

                if len(heading_text) >= 12:
                    article_title = heading_text

            description_tag = (
                article_soup.find(
                    "meta", attrs={"name": "description"}
                )
                or article_soup.find(
                    "meta", attrs={"property": "og:description"}
                )
            )

            if description_tag:
                description = description_tag.get("content", "")

            if not description:
                main_content = (
                    article_soup.find("main")
                    or article_soup.find("article")
                )

                if main_content:
                    description = " ".join(
                        main_content.get_text(" ", strip=True).split()
                    )

        except requests.RequestException as exc:
            print(
                f"[WARN] Could not fetch article {canonical}: {exc}",
                flush=True
            )
            continue

        relevant_text = (
            article_title + " " + path + " " + description[:1500]
        ).lower()

        if not any(word in relevant_text for word in OUTBREAK_KEYWORDS):
            continue

        if not is_2026_report(published):
            print(
                f"[DEBUG] HTML report excluded: "
                f"{article_title!r}; date={published!r}",
                flush=True
            )
            continue

        results.append({
            "title": article_title[:500],
            "link": canonical,
            "description": description[:5000],
            "published": parse_date(published),
        })

        if len(results) >= MAX_ENTRIES_PER_SOURCE:
            break

    print(
        f"[INFO] HTML scraper checked {checked} candidate pages; "
        f"found {len(results)} dated 2026 reports from {source_host}",
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
            if source_type == "who_api":
                entries = extract_who_api(source["url"])

            elif source_type == "web_scrape":
                entries = extract_web_page(source["url"])

            elif source_type == "rss":
                feed = feedparser.parse(
                    source["url"],
                    request_headers={"User-Agent": USER_AGENT}
                )

                entries = []

                for item in feed.entries[:MAX_ENTRIES_PER_SOURCE]:
                    raw_date = (
                        item.get("published")
                        or item.get("updated")
                        or ""
                    )

                    published = parse_date(raw_date)

                    if not is_2026_report(published):
                        continue

                    entries.append({
                        "title": item.get("title", "Untitled"),
                        "link": item.get("link", ""),
                        "description": (
                            item.get("summary")
                            or item.get("description")
                            or ""
                        ),
                        "published": published
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
