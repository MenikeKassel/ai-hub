"""Additive storage contract for research observations and delivery."""

RESEARCH_MIGRATION = """
CREATE TABLE IF NOT EXISTS post_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id TEXT NOT NULL REFERENCES posts(post_id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    source_updated_at TEXT NOT NULL DEFAULT '',
    fetched_at TEXT NOT NULL,
    accepted INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL,
    UNIQUE(post_id,provider,content_hash,source_updated_at)
);
CREATE TABLE IF NOT EXISTS post_observation_heads (
    post_id TEXT PRIMARY KEY REFERENCES posts(post_id) ON DELETE CASCADE,
    observation_id INTEGER NOT NULL REFERENCES post_observations(id)
);
CREATE INDEX IF NOT EXISTS idx_post_observations_post ON post_observations(post_id,id);
CREATE TABLE IF NOT EXISTS collection_surface_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    kol_id INTEGER NOT NULL REFERENCES kols(id),
    platform TEXT NOT NULL,
    surface TEXT NOT NULL,
    status TEXT NOT NULL,
    requested_count INTEGER NOT NULL DEFAULT 0,
    received_count INTEGER NOT NULL DEFAULT 0,
    pages INTEGER NOT NULL DEFAULT 0,
    exhausted INTEGER NOT NULL DEFAULT 0,
    historical_complete INTEGER NOT NULL DEFAULT 0,
    earliest_posted_at TEXT NOT NULL DEFAULT '',
    latest_posted_at TEXT NOT NULL DEFAULT '',
    warnings_json TEXT NOT NULL DEFAULT '[]',
    origin TEXT NOT NULL DEFAULT 'live_capture',
    recorded_at TEXT NOT NULL,
    UNIQUE(run_id,kol_id,surface)
);
CREATE INDEX IF NOT EXISTS idx_surface_runs_latest ON collection_surface_runs(kol_id,surface,id DESC);
CREATE TABLE IF NOT EXISTS research_jobs (
    post_id TEXT PRIMARY KEY REFERENCES posts(post_id) ON DELETE CASCADE,
    generation INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'queued',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    queued_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS research_themes (
    theme_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    terms_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS theme_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    term TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','accepted','rejected')),
    theme_id TEXT NOT NULL DEFAULT '',
    first_detected_at TEXT NOT NULL,
    last_detected_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS theme_candidate_evidence (
    candidate_id INTEGER NOT NULL REFERENCES theme_candidates(id),
    post_id TEXT NOT NULL REFERENCES posts(post_id),
    observation_id INTEGER NOT NULL DEFAULT 0,
    evidence_json TEXT NOT NULL,
    kind TEXT NOT NULL,
    source_role TEXT NOT NULL,
    PRIMARY KEY(candidate_id,post_id)
);
CREATE TABLE IF NOT EXISTS research_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    post_id TEXT NOT NULL DEFAULT '',
    theme_id TEXT NOT NULL DEFAULT '',
    theme_name TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    published_at TEXT NOT NULL DEFAULT '',
    delivery_date TEXT NOT NULL DEFAULT '',
    acknowledged_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_research_delivery ON research_items(delivery_date,id DESC);
CREATE INDEX IF NOT EXISTS idx_candidate_evidence_post ON theme_candidate_evidence(post_id);
CREATE INDEX IF NOT EXISTS idx_research_item_source ON research_items(post_id,theme_id);
CREATE TABLE IF NOT EXISTS research_state (name TEXT PRIMARY KEY,value TEXT NOT NULL);
ALTER TABLE theme_leads ADD COLUMN observation_id INTEGER NOT NULL DEFAULT 0;
ALTER TABLE theme_leads ADD COLUMN evidence_at TEXT NOT NULL DEFAULT '';
ALTER TABLE theme_extractions ADD COLUMN observation_id INTEGER NOT NULL DEFAULT 0;
CREATE TRIGGER IF NOT EXISTS research_post_insert AFTER INSERT ON posts BEGIN
    INSERT INTO research_jobs(post_id) VALUES(NEW.post_id)
    ON CONFLICT(post_id) DO UPDATE SET generation=generation+1,status='queued',next_retry_at='',attempts=0,error='',updated_at=CURRENT_TIMESTAMP;
END;
CREATE TRIGGER IF NOT EXISTS research_post_update AFTER UPDATE OF content_hash,updated_at ON posts
WHEN NEW.content_hash<>OLD.content_hash OR NEW.updated_at<>OLD.updated_at BEGIN
    DELETE FROM post_observation_heads WHERE post_id=NEW.post_id AND NEW.content_hash<>OLD.content_hash;
    INSERT INTO research_jobs(post_id) VALUES(NEW.post_id)
    ON CONFLICT(post_id) DO UPDATE SET generation=generation+1,status='queued',next_retry_at='',attempts=0,error='',updated_at=CURRENT_TIMESTAMP;
END;
CREATE TRIGGER IF NOT EXISTS research_head_insert AFTER INSERT ON post_observation_heads BEGIN
    INSERT INTO research_jobs(post_id) VALUES(NEW.post_id)
    ON CONFLICT(post_id) DO UPDATE SET generation=generation+1,status='queued',next_retry_at='',attempts=0,error='',updated_at=CURRENT_TIMESTAMP;
END;
CREATE TRIGGER IF NOT EXISTS research_head_update AFTER UPDATE ON post_observation_heads
WHEN NEW.observation_id<>OLD.observation_id BEGIN
    INSERT INTO research_jobs(post_id) VALUES(NEW.post_id)
    ON CONFLICT(post_id) DO UPDATE SET generation=generation+1,status='queued',next_retry_at='',attempts=0,error='',updated_at=CURRENT_TIMESTAMP;
END;
CREATE TRIGGER IF NOT EXISTS research_classification_insert AFTER INSERT ON classifications BEGIN
    INSERT INTO research_jobs(post_id) VALUES(NEW.post_id)
    ON CONFLICT(post_id) DO UPDATE SET generation=generation+1,status='queued',next_retry_at='',attempts=0,error='',updated_at=CURRENT_TIMESTAMP;
END;
CREATE TRIGGER IF NOT EXISTS research_classification_update AFTER UPDATE ON classifications BEGIN
    INSERT INTO research_jobs(post_id) VALUES(NEW.post_id)
    ON CONFLICT(post_id) DO UPDATE SET generation=generation+1,status='queued',next_retry_at='',attempts=0,error='',updated_at=CURRENT_TIMESTAMP;
END;
"""
