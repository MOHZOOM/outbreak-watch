CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    original_url TEXT NOT NULL UNIQUE,
    source_name TEXT NOT NULL,
    published_at TEXT,
    collected_at TEXT NOT NULL,
    description TEXT,
    is_relevant INTEGER NOT NULL DEFAULT 0,
    category TEXT NOT NULL DEFAULT 'Unclassified',
    disease_event TEXT NOT NULL DEFAULT 'Unknown',
    country_region TEXT NOT NULL DEFAULT 'Unknown',
    event_date TEXT NOT NULL DEFAULT 'Unknown',
    outbreak_status TEXT NOT NULL DEFAULT 'Unknown',
    ai_summary TEXT NOT NULL DEFAULT '',
    ai_processed INTEGER NOT NULL DEFAULT 0,
    duplicate_of INTEGER,
    FOREIGN KEY(duplicate_of) REFERENCES articles(id)
);
CREATE INDEX IF NOT EXISTS idx_articles_published_at ON articles(published_at);
CREATE INDEX IF NOT EXISTS idx_articles_relevant ON articles(is_relevant);
