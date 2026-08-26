-- PostgreSQL URL Shortener schema
-- __TABLE_KIND__ is substituted at startup with either "TABLE" or "UNLOGGED TABLE"
-- (see app/database.py). UNLOGGED tables skip WAL writes for much faster
-- inserts/updates at the cost of being wiped on a crash/unclean restart --
-- fine for a short-code mapping table you can regenerate, tune via
-- URLSHORT_UNLOGGED_TABLE=false if you need crash-durability instead.

CREATE __TABLE_KIND__ IF NOT EXISTS urls (
    id               BIGSERIAL PRIMARY KEY,
    short_code       VARCHAR(32) NOT NULL UNIQUE,
    long_url         TEXT NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at       TIMESTAMPTZ,
    hits             BIGINT NOT NULL DEFAULT 0,
    last_accessed_at TIMESTAMPTZ
);

-- The unique index backing short_code lookups is what keeps resolution fast:
-- hot rows stay resident in PostgreSQL's shared_buffers, so repeated lookups
-- of popular codes are served from memory rather than disk.
CREATE UNIQUE INDEX IF NOT EXISTS idx_urls_short_code ON urls (short_code);
CREATE INDEX IF NOT EXISTS idx_urls_expires_at ON urls (expires_at) WHERE expires_at IS NOT NULL;

-- Notify application instances whenever a row is updated or deleted, so the
-- in-process LRU cache in every replica can evict the stale entry instead of
-- serving outdated long_urls after an edit/delete.
CREATE OR REPLACE FUNCTION notify_url_change() RETURNS TRIGGER AS $$
BEGIN
    PERFORM pg_notify('url_cache_invalidate', COALESCE(NEW.short_code, OLD.short_code));
    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_url_change ON urls;
CREATE TRIGGER trg_url_change
AFTER UPDATE OR DELETE ON urls
FOR EACH ROW EXECUTE FUNCTION notify_url_change();
