# Outbreak Watch — MVP Starter

A small, source-first website for collecting, classifying, summarizing, and archiving public outbreak-investigation news.

## What this starter includes
- Flask website with **Latest News** and **Archive** pages.
- SQLite storage for collected items.
- RSS ingestion with a configurable source list.
- Optional OpenAI API processing for relevance, structured fields, and short summaries.
- Basic duplicate detection using normalized URLs and title similarity.
- Search and filters for country, disease/event, category, and date.
- Original source name and original URL retained and displayed on every item.
- Sample/demo records are clearly marked as examples; they are not presented as verified current news.

## Recommended first-version architecture
1. **Collection:** The starter supports RSS/Atom feeds and the documented WHO Disease Outbreak News JSON endpoint. Add other official APIs and carefully scoped HTML parsers only where feeds are unavailable.
2. **Normalization:** Convert each item to a common schema and retain the publisher's title, publication date, source name, and original URL.
3. **Deduplication:** Exact canonical URL first, then normalized-title similarity. Keep multiple source links when reports concern the same event.
4. **AI triage:** Run a low-cost model only on new/changed items. Return structured JSON: relevance, disease/event, country/region, event date, outbreak status, category, and a concise summary. Do not let AI invent missing facts; use `Unknown` where needed.
5. **Persistence:** Store raw metadata, extracted text where permitted, AI fields, timestamps, and processing status in SQLite for the MVP; move to PostgreSQL for production.
6. **Review and publication:** In the MVP, display automatically processed results with source attribution and an explicit note that summaries are machine-generated. Add a human review queue before any operational/public-health use.
7. **Scheduling and hosting:** Run ingestion on a scheduled worker (e.g., every 30–60 minutes) rather than on a web-page request.

## Run locally
Python 3.11+ recommended.

```bash
python -m venv .venv
source .venv/bin/activate        # macOS/Linux
# .venv\\Scripts\\activate       # Windows
pip install -r requirements.txt
cp .env.example .env
python app.py
```

Open http://127.0.0.1:5000

The website can run without an AI key; the fallback rules label the item using keyword heuristics and leave the summary blank rather than fabricate one.

## Configure AI (optional)
1. Create an API key in the provider account and enable billing/usage limits.
2. Copy it into `.env` as `OPENAI_API_KEY=...`.
3. Set `OPENAI_MODEL` to a currently available, cost-effective model in your account.
4. Never commit `.env` or expose the API key in frontend JavaScript.

The code calls the OpenAI Responses API from the server. Confirm model availability and pricing in the official pricing page before deployment: https://developers.openai.com/api/docs/pricing

## Configure sources
The default configuration uses WHO's documented Disease Outbreak News JSON endpoint (`https://cms.who.int/api/news/diseaseoutbreaknews`). WHO documents the endpoint and its fields here: https://cms.who.int/api/news/diseaseoutbreaknews/sfhelp. The optional WHO AFRO entry is disabled because its page is a feed directory, not a verified feed URL. Edit `sources.json` to add exact verified feed endpoints. Prefer official sources and feeds with clear reuse terms. Confirm the feed URL, terms of use, rate limits, and attribution requirements before production. Some publishers provide only web pages or newsletters; do not scrape pages that disallow automated access.

## MVP scope and limitations
- This is a development starter, not a validated public-health surveillance system.
- RSS availability and publisher metadata vary; location, disease, and outbreak status may be missing.
- AI classifications and summaries can be wrong. The original link is the authoritative source.
- The demo parser uses feed-provided titles/descriptions only; it does not bypass paywalls or scrape full articles.
- Before public launch, add tests, logging/monitoring, backups, rate limiting, accessibility review, privacy/security checks, and a human verification workflow.


## Deploy to Render (supervisor demo)
- Push the contents of this project folder to a GitHub repository.
- In Render, create a new Blueprint and select the repository; Render reads `render.yaml`.
- The included Blueprint uses a small persistent disk mounted at `/var/data` for the SQLite archive. Persistent disks may require a paid Render plan; check current Render pricing before deploying.
- The web process runs one Gunicorn worker and a simple in-process collection loop, which attempts to collect on startup and every 30 minutes while the service is running. On hosting plans that sleep, collection pauses while the service sleeps.
- AI is disabled by default. To enable it, set `AI_ENABLED=true` and add `OPENAI_API_KEY` as a secret environment variable in Render. Confirm current model availability and set `OPENAI_MODEL` as needed.
- Verify that live WHO records appear, then test the archive and original links. API field formats and availability can change, so check Render logs if collection fails.
- This is an MVP demonstration setup. For reliable production use, replace the in-process loop with a dedicated worker/scheduler and use a shared managed database; add monitoring, tests, source terms review, and human review.


### Free demo deployment (temporary archive)
The included `render.yaml` uses Render's Free web-service plan so you can try the site without choosing a paid compute plan. Free services have an ephemeral filesystem and may spin down after inactivity; SQLite archive data can be lost after restart/redeploy/spin-down. This is suitable only for an initial demo. For a reliable archive, choose a paid service with a persistent disk or use a managed database after approving the cost. Review current limits: https://render.com/docs/free
