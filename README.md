# URL Shortener

A small, containerized URL shortener built with **FastAPI** + **asyncpg**.
There is **no Redis, Memcached, or any other cache service** — fast
resolution comes entirely from PostgreSQL plus a thin in-process cache that
Postgres itself keeps consistent.

## How the "caching" works

1. **PostgreSQL's shared buffer cache.** `urls.short_code` has a unique
   index, so hot rows stay resident in Postgres's own `shared_buffers`.
   Repeated lookups of popular codes are served from RAM by Postgres, not
   disk — no extra infrastructure required.
2. **In-process LRU cache.** Each app instance also keeps a small
   in-memory LRU (`app/cache.py`) of the most recently resolved codes, so
   the hottest redirects skip the DB round trip entirely.
3. **Cache coherency via `LISTEN`/`NOTIFY`.** A Postgres trigger fires
   `pg_notify('url_cache_invalidate', short_code)` on every `UPDATE` or
   `DELETE` against `urls`. Every app instance keeps a dedicated listener
   connection and evicts the matching key the moment it's notified — so
   edits/deletes propagate correctly across replicas with Postgres itself
   acting as the invalidation bus. No cache-service, no stale-read window
   beyond typical NOTIFY latency (milliseconds).
4. **Batched hit counting.** Redirect hits are buffered in memory and
   flushed to Postgres as one batched `UPDATE` every
   `URLSHORT_HIT_FLUSH_INTERVAL_SECONDS` (default 2s), so the hot redirect
   path never blocks on a write.
5. **UNLOGGED table (optional, default on).** The `urls` table is created
   `UNLOGGED` by default, which skips WAL writes for faster inserts/updates.
   Trade-off: an unclean Postgres crash wipes the table. Set
   `URLSHORT_UNLOGGED_TABLE=false` if you need crash durability instead.

## Project layout

```
url-shortener/
├── app/
│   ├── main.py        # FastAPI routes (shorten, resolve, stats, delete, health)
│   ├── config.py       # Settings, all sourced from env vars
│   ├── database.py     # asyncpg connection pool + schema migration
│   ├── cache.py         # In-process LRU cache + LISTEN/NOTIFY invalidation
│   ├── schemas.py       # Pydantic request/response models
│   └── utils.py         # Short-code generation/validation
├── sql/
│   └── init.sql         # Schema, index, trigger (idempotent, runs on startup)
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
└── .dockerignore
```

## Configuration (no hardcoded URLs/values)

Everything is read from environment variables at runtime, so the same image
runs anywhere without code changes. See `.env.example` for the full list;
key ones:

| Variable | Purpose | Default |
|---|---|---|
| `URLSHORT_DATABASE_URL` | Postgres DSN | `postgresql://postgres:postgres@localhost:5432/urlshortener` |
| `URLSHORT_BASE_URL` | Public base URL used to build `short_url` in responses | `http://localhost:8000` |
| `URLSHORT_SHORT_CODE_LENGTH` | Length of generated codes | `7` |
| `URLSHORT_UNLOGGED_TABLE` | Use `UNLOGGED TABLE` for speed | `true` |
| `URLSHORT_CACHE_MAX_SIZE` | Max entries in the per-instance LRU cache | `10000` |
| `URLSHORT_HIT_FLUSH_INTERVAL_SECONDS` | How often buffered hit counters are flushed | `2.0` |
| `URLSHORT_DEFAULT_EXPIRY_DAYS` | Optional default TTL for new links | unset (never expires) |

When run via `docker-compose`, the compose file maps friendlier
`POSTGRES_*` / `BASE_URL` / etc. variables (see `.env.example`) into the
`URLSHORT_*` variables the app reads.

## Running it

```bash
cp .env.example .env
# edit .env if you want different ports/credentials/base URL
docker compose up --build
```

This starts:
- `db`: Postgres 16, with a persisted volume
- `app`: the FastAPI service, waiting for Postgres to be healthy, applying
  the schema automatically on startup

The API is then available at `http://localhost:8000` (or whatever
`APP_PORT`/`BASE_URL` you configured).

### Running without Docker

```bash
pip install -r requirements.txt
export URLSHORT_DATABASE_URL="postgresql://postgres:postgres@localhost:5432/urlshortener"
export URLSHORT_BASE_URL="http://localhost:8000"
uvicorn app.main:app --reload
```

## API

### Create a short link
```
POST /api/shorten
Content-Type: application/json

{
  "url": "https://example.com/some/very/long/path",
  "custom_code": "my-code",       // optional
  "expires_in_days": 30            // optional
}
```
Response `201`:
```json
{
  "short_code": "my-code",
  "short_url": "http://localhost:8000/my-code",
  "long_url": "https://example.com/some/very/long/path",
  "expires_at": "2026-09-25T12:00:00Z"
}
```

### Resolve / redirect
```
GET /{code}
```
Issues a `307` redirect to the stored long URL, or `404` if unknown, or
`410` if expired. This is the hot path served by the cache layer.

### Stats
```
GET /api/stats/{code}
```
Returns `long_url`, `created_at`, `expires_at`, `hits`, `last_accessed_at`.

### Delete
```
DELETE /api/{code}
```
Removes the mapping and invalidates it everywhere via `NOTIFY`.

### Health
```
GET /health
```
Checks DB connectivity and reports the current in-process cache size.

## Scaling notes

- The app is stateless aside from the in-process cache, so you can run
  multiple replicas behind a load balancer; each keeps its own LRU cache
  in sync via Postgres `LISTEN`/`NOTIFY`.
- Tune `URLSHORT_CACHE_MAX_SIZE` and Postgres's own `shared_buffers` for
  your traffic/dataset size — the bigger your working set of "hot" codes,
  the more value both caches provide.
- If you need durability guarantees beyond what `UNLOGGED TABLE` gives you,
  set `URLSHORT_UNLOGGED_TABLE=false`; you'll trade some write throughput
  for crash-safety.
