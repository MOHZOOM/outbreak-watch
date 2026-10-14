import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, urljoin

import feedparser
import requests
from rapidfuzz.fuzz import ratio
from ai_processor import classify_and_summarize

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = os.getenv("DATABASE_PATH", str(BASE_DIR / "outbreak_watch.db"))
USER_AGENT = "OutbreakWatchMVP/0.1 (public-health news aggregator; contact site administrator)"

def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def canonical_url(url: str) -> str:
    parts = urlsplit((url or "").strip())
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript((BASE_DIR / "schema.sql").read_text(encoding="utf-8"))

def load_sources():
    return json.loads((BASE_DIR / "sources.json").read_text(encoding="utf-8"))


def extract_who_api(url):
    """Use WHO's Disease Outbreak News JSON endpoint."""
    response = requests.get(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json"
        },
        timeout=25
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

        # Repair WHO DON links that contain only a report code.
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
            str(item.get(k) or "")
            for k in ("Summary", "Overview", "Epidemiology", "Assessment")
            if item.get(k)
        )

        if link:
            results.append({
                "title": str(title).strip(),
                "link": link,
                "description": description.strip(),
                "published": str(published)
            })

    return results


def collect_once():
    init_db()
    new_count = 0
    for source in load_sources():
        if not source.get("enabled"):
            continue
        try:
            if source.get("type") == "rss":
                parsed = feedparser.parse(source["url"], request_headers={"User-Agent": USER_AGENT})
                if parsed.bozo and not parsed.entries:
                    print(f"[WARN] Could not read feed: {source['name']} ({parsed.bozo_exception})")
                    continue
                entries = [{
                    "title": (entry.get("title") or "Untitled").strip(),
                    "link": entry.get("link", ""),
                    "description": entry.get("summary", "") or entry.get("description", ""),
                    "published": entry.get("published") or entry.get("updated") or ""
                } for entry in parsed.entries[:100]]
            elif source.get("type") == "who_api":
                entries = extract_who_api(source["url"])[:100]
            else:
                print(f"[WARN] Unsupported source type: {source.get('type')}")
                continue
        except Exception as exc:
            print(f"[WARN] Source failed: {source['name']}: {exc}")
            continue

        for entry in entries:
            title = entry["title"]
            link = canonical_url(entry["link"])
            if not link:
                continue
            description = entry.get("description", "")
            published = entry.get("published", "")
            with sqlite3.connect(DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                if conn.execute("SELECT id FROM articles WHERE original_url=?", (link,)).fetchone():
                    continue
                possible = conn.execute(
                    "SELECT id, title FROM articles WHERE collected_at >= datetime('now','-30 days')"
                ).fetchall()
                duplicate_id = None
                for old in possible:
                    if ratio(title.lower(), old["title"].lower()) >= 94:
                        duplicate_id = old["id"]
                        break
                fields = classify_and_summarize(title, description, source["name"], published)
                conn.execute("""
                    INSERT OR IGNORE INTO articles
                    (title, original_url, source_name, published_at, collected_at, description,
                     is_relevant, category, disease_event, country_region, event_date,
                     outbreak_status, ai_summary, ai_processed, duplicate_of)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    title, link, source["name"], published, utc_now(), description[:10000],
                    int(fields["is_relevant"]), fields["category"], fields["disease_event"],
                    fields["country_region"], fields["event_date"], fields["outbreak_status"],
                    fields["summary"], int(fields.get("_ai_processed", False)),
                    duplicate_id
                ))
                if conn.total_changes:
                    new_count += 1
    return new_count

if __name__ == "__main__":
    print(f"Collected {collect_once()} new records.")
