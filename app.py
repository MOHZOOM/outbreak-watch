import os
import sqlite3
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

from flask import Flask, render_template, request
from collector import init_db, DB_PATH, collect_once

BASE_DIR = Path(__file__).resolve().parent
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "local-development-only")
init_db()

# Simple MVP scheduler: collects on startup and then approximately every 30 minutes.
# Keep one Gunicorn worker for this in-process scheduler. For production scale, move
# collection into a dedicated scheduled worker and use a shared PostgreSQL database.
_collection_lock = threading.Lock()
_scheduler_started = False
# Shown on the site so visitors know when a collection is still running
# (for example right after the free Render service wakes up).
collection_status = {"running": False, "last_finished": None, "last_new": None}

def collection_loop():
    while True:
        try:
            with _collection_lock:
                collection_status["running"] = True
                count = collect_once()
                collection_status["last_new"] = count
                app.logger.info("Scheduled collection completed: %s new records", count)
        except Exception:
            app.logger.exception("Scheduled collection failed")
        finally:
            collection_status["running"] = False
            collection_status["last_finished"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        time.sleep(int(os.getenv("COLLECTION_INTERVAL_SECONDS", "1800")))

def start_scheduler():
    global _scheduler_started
    if os.getenv("DISABLE_SCHEDULER", "false").lower() == "true":
        return
    if not _scheduler_started:
        _scheduler_started = True
        thread = threading.Thread(target=collection_loop, name="outbreak-collector", daemon=True)
        thread.start()

start_scheduler()

def get_articles():
    query = request.args.get("q", "").strip()
    country = request.args.get("country", "").strip()
    disease = request.args.get("disease", "").strip()
    category = request.args.get("category", "").strip()
    source = request.args.get("source", "").strip()
    params = []
    clauses = ["is_relevant = 1"]
    if query:
        clauses.append("(title LIKE ? OR description LIKE ? OR ai_summary LIKE ? OR source_name LIKE ?)")
        params += [f"%{query}%"] * 4
    if country:
        clauses.append("country_region LIKE ?")
        params.append(f"%{country}%")
    if disease:
        clauses.append("disease_event LIKE ?")
        params.append(f"%{disease}%")
    if category:
        clauses.append("category = ?")
        params.append(category)
    if source:
        clauses.append("source_name = ?")
        params.append(source)
    where = " AND ".join(clauses)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT * FROM articles WHERE {where} ORDER BY COALESCE(published_at, collected_at) DESC LIMIT 500", params
        ).fetchall()
    return rows

def query(sql, params=()):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql, params).fetchall()

@app.context_processor
def site_stats():
    row = query(
        """SELECT COUNT(*) AS total,
                  COUNT(DISTINCT NULLIF(country_region, 'Unknown')) AS locations,
                  COUNT(DISTINCT source_name) AS sources,
                  MAX(collected_at) AS last_collected,
                  SUM(published_at >= date('now', '-7 days')) AS this_week,
                  SUM(published_at >= date('now', '-30 days')) AS this_month
           FROM articles WHERE is_relevant = 1"""
    )[0]
    source_names = [r["source_name"] for r in query(
        "SELECT DISTINCT source_name FROM articles WHERE is_relevant = 1 ORDER BY 1"
    )]
    present_categories = {r["category"] for r in query(
        "SELECT DISTINCT category FROM articles WHERE is_relevant = 1"
    )}
    return {
        "stats": row,
        "source_names": source_names,
        "status": collection_status,
        "categories": [c for c in CATEGORIES if c in present_categories],
        "filtering": any(request.args.get(k) for k in ("q", "country", "disease", "category", "source")),
    }

CATEGORIES = [
    "Outbreak investigation", "Emerging public health threat",
    "Official public health update", "Research / surveillance",
]

@app.template_filter("ago")
def ago(value):
    """'2026-10-23' -> 'Today' / '3 days ago' / '23 Oct 2026'."""
    try:
        day = date.fromisoformat(str(value)[:10])
    except ValueError:
        return value or "Date not supplied"
    days = (date.today() - day).days
    if days <= 0:
        return "Today"
    if days == 1:
        return "Yesterday"
    if days < 14:
        return f"{days} days ago"
    return f"{day.day} {day:%b %Y}"

@app.template_filter("slug")
def slug(value):
    return "".join(c if c.isalnum() else "-" for c in str(value).lower()).strip("-")

@app.route("/")
def latest():
    return render_template("index.html", articles=get_articles()[:60], active="latest")

@app.route("/archive")
def archive():
    return render_template("archive.html", articles=get_articles(), active="archive")

@app.route("/health")
def health():
    return {"status": "ok"}, 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
