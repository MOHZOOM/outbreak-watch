import json
import os
import re
import sqlite3
import subprocess
import sys
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


def verify_article_url(url):
    """Return (final URL, page publication date), or empty values for an invalid page."""
    if not url or urlsplit(url).scheme not in {"http", "https"}:
        return "", ""

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
            return "", ""

        # Some sites display a branded not-found page with HTTP 200.
        page_text = BeautifulSoup(response.text, "html.parser").get_text(
            " ", strip=True
        ).lower()
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
            return "", ""

        final_url = response.url
        if urlsplit(final_url).scheme not in {"http", "https"}:
            return "", ""

        # When available, cross-check the date shown on the actual article page.
        page_published = extract_page_date(
            BeautifulSoup(response.text, "html.parser")
        )
        return final_url, page_published
    except requests.RequestException as exc:
        print(f"[WARN] Could not verify source URL {url}: {exc}", flush=True)
        return "", ""


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
    """Read WHO records, retain 2026 reports, and verify their source URLs."""
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

        if not link:
            print(
                f"[WARN] WHO report skipped because it has no source URL: {title!r}",
                flush=True,
            )
            continue

        verified_link, page_published = verify_article_url(link)
        if not verified_link:
            print(
                f"[WARN] WHO report skipped because its URL could not be verified: {title!r}",
                flush=True,
            )
            continue

        # Trust the article page's own publication date when available; the API
        # date can otherwise make an older article appear to be from 2026.
        if page_published:
            if page_published != parsed_date:
                print(
                    f"[INFO] WHO date cross-check for {title!r}: "
                    f"API={parsed_date!r}, page={page_published!r}; using page date.",
                    flush=True,
                )
            parsed_date = page_published

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

        if verified_link:
            results.append({
                "title": str(title).strip(),
                "link": verified_link,
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



def extract_with_scrapegraphai(listing_url):
    """Use ScrapeGraphAI to discover candidate articles, then verify them with HTML metadata.

    This is optional and enabled with SCRAPEGRAPHAI_ENABLED=true. ScrapeGraphAI's
    extracted dates are never trusted by themselves; each candidate article page
    must provide a verifiable publication date from its HTML metadata.
    """
    if os.getenv("SCRAPEGRAPHAI_ENABLED", "false").strip().lower() not in {
        "1", "true", "yes", "on"
    }:
        return []

    api_key = os.getenv("OPENAI_API_KEY") or os.getenv("OPENAI_APIKEY")
    if not api_key:
        print(
            "[WARN] ScrapeGraphAI is enabled but OPENAI_API_KEY/OPENAI_APIKEY is missing; "
            "skipping AI-assisted scraping.",
            flush=True,
        )
        return []

    try:
        from scrapegraphai.graphs import SmartScraperGraph
    except ImportError:
        print(
            "[WARN] ScrapeGraphAI is enabled but scrapegraphai is not installed.",
            flush=True,
        )
        return []

    model = os.getenv("SCRAPEGRAPHAI_MODEL", "openai/gpt-4o-mini")
    prompt = """
    Extract candidate outbreak-news articles linked from this official health website.
    Return JSON with an 'articles' array. Each item must contain:
    title (string), url (absolute article URL), and short_description (string).
    Include only links to individual news/report articles on the same website.
    Do not invent titles or URLs. Do not infer or invent publication dates.
    """

    def run_graph():
        graph = SmartScraperGraph(
            prompt=prompt,
            source=listing_url,
            config={
                "llm": {
                    "api_key": api_key,
                    "model": model,
                    "temperature": 0,
                },
                "verbose": False,
                "headless": True,
            },
        )
        return graph.run()

    try:
        extracted = run_graph()
    except Exception as exc:
        error_text = str(exc)
        missing_browser = (
            "Executable doesn't exist" in error_text
            or "Executable does not exist" in error_text
            or "executable doesn't exist" in error_text.lower()
        )

        # Render may build successfully but run without the browser binary in
        # Playwright's cache. Recover only this failure and retry once.
        if missing_browser:
            browser_path = os.getenv(
                "PLAYWRIGHT_BROWSERS_PATH",
                str(Path.home() / ".cache" / "ms-playwright"),
            )
            print(
                "[WARN] ScrapeGraphAI browser executable is missing. "
                f"Playwright cache: {browser_path!r}. Attempting one Chromium install "
                "in the running service before retrying.",
                flush=True,
            )
            try:
                install = subprocess.run(
                    [sys.executable, "-m", "playwright", "install", "chromium"],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=240,
                )
                if install.stdout.strip():
                    print("[INFO] Playwright install output: " + install.stdout[-3000:], flush=True)
                if install.stderr.strip():
                    print("[INFO] Playwright install details: " + install.stderr[-3000:], flush=True)
                extracted = run_graph()
            except Exception as retry_exc:
                print(
                    "[WARN] ScrapeGraphAI retry after Chromium recovery failed for "
                    f"{listing_url}: {retry_exc}. Continuing with the regular HTML scraper.",
                    flush=True,
                )
                return []
        else:
            print(f"[WARN] ScrapeGraphAI failed for {listing_url}: {exc}", flush=True)
            return []

    if isinstance(extracted, dict):
        candidates = extracted.get("articles", [])
    elif isinstance(extracted, list):
        candidates = extracted
    else:
        candidates = []

    listing_host = (urlsplit(listing_url).hostname or "").lower()
    allowed_hosts = {listing_host}
    if listing_host.startswith("www."):
        allowed_hosts.add(listing_host[4:])
    else:
        allowed_hosts.add("www." + listing_host)

    results = []
    seen = set()
    for item in candidates[:MAX_ENTRIES_PER_SOURCE]:
        if not isinstance(item, dict):
            continue
        title = " ".join(str(item.get("title") or "").split())
        candidate_url = str(item.get("url") or item.get("link") or "").strip()
        if not title or not candidate_url:
            continue

        absolute_url = urljoin(listing_url, candidate_url)
        parts = urlsplit(absolute_url)
        if parts.scheme not in {"http", "https"} or (parts.hostname or "").lower() not in allowed_hosts:
            print(f"[WARN] ScrapeGraphAI candidate rejected (off-site URL): {absolute_url}", flush=True)
            continue

        link = canonical_url(absolute_url)
        if link in seen or link == canonical_url(listing_url):
            continue
        seen.add(link)

        try:
            response = fetch_page(link)
            soup = BeautifulSoup(response.text, "html.parser")
        except requests.RequestException as exc:
            print(f"[WARN] ScrapeGraphAI candidate URL failed: {link}: {exc}", flush=True)
            continue

        # A HTTP 200 page can still be a publisher's not-found page.
        page_text = " ".join(soup.get_text(" ", strip=True).lower().split())[:5000]
        if any(marker in page_text for marker in (
            "page cannot be found", "this page cannot be found", "page not found", "404 not found"
        )):
            print(f"[WARN] ScrapeGraphAI candidate rejected (not-found page): {link}", flush=True)
            continue

        verified_date = extract_page_date(soup)
        if not is_2026_report(verified_date):
            print(
                f"[DEBUG] ScrapeGraphAI candidate rejected; verified page date is not 2026: "
                f"title={title!r}; date={verified_date!r}; url={link}",
                flush=True,
            )
            continue

        heading = soup.find(["h1", "h2"])
        if heading:
            heading_text = " ".join(heading.get_text(" ", strip=True).split())
            if len(heading_text) >= 8:
                title = heading_text

        desc = item.get("short_description") or item.get("description") or ""
        if not desc:
            meta = soup.find("meta", attrs={"name": "description"}) or soup.find(
                "meta", attrs={"property": "og:description"}
            )
            if meta:
                desc = meta.get("content", "")

        if not any(word in (title + " " + str(desc)).lower() for word in OUTBREAK_KEYWORDS):
            continue

        results.append({
            "title": title[:500],
            "link": link,
            "description": str(desc)[:5000],
            "published": verified_date,
        })

    print(
        f"[INFO] ScrapeGraphAI verified {len(results)} dated 2026 articles "
        f"from {listing_host}",
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

    # Remove the known broken, title-generated WHO URL from older database runs.
    # Do not substitute an unrelated 2025 report for a claimed 2026 report.
    broken_mers_url = "%middle-east-respiratory-syndrome-coronavirus---kingdom-of-saudi-arabia%"
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        stale_rows = conn.execute(
            "SELECT id, title FROM articles WHERE lower(original_url) LIKE lower(?)",
            (broken_mers_url,),
        ).fetchall()
        if stale_rows:
            conn.execute(
                "DELETE FROM articles WHERE lower(original_url) LIKE lower(?)",
                (broken_mers_url,),
            )
            print(
                f"[WARN] Removed {len(stale_rows)} stale record(s) with the known broken MERS URL. "
                "They will only return if a verified source is found.",
                flush=True,
            )

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
                # Keep deterministic HTML scraping as the baseline. When enabled,
                # ScrapeGraphAI contributes additional candidates that are verified
                # against the real article page before they can be stored.
                entries = extract_web_page(source["url"])
                sg_entries = extract_with_scrapegraphai(source["url"])
                known_links = {canonical_url(entry.get("link", "")) for entry in entries}
                entries.extend(
                    entry for entry in sg_entries
                    if canonical_url(entry.get("link", "")) not in known_links
                )

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
