import os
import sqlite3
import threading
import time
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

def collection_loop():
    while True:
        try:
            with _collection_lock:
                count = collect_once()
                app.logger.info("Scheduled collection completed: %s new records", count)
        except Exception:
            app.logger.exception("Scheduled collection failed")
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
    where = " AND ".join(clauses)
    order = "COALESCE(published_at, collected_at) DESC"
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT * FROM articles WHERE {where} ORDER BY COALESCE(published_at, collected_at) DESC LIMIT 500", params
        ).fetchall()
    return rows

@app.route("/")
def latest():
    return render_template("index.html", articles=get_articles(), active="latest")

@app.route("/archive")
def archive():
    return render_template("archive.html", articles=get_articles(), active="archive")

@app.route("/health")
def health():
    return {"status": "ok"}, 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
