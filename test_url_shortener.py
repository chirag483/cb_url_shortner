"""
Unit tests for the PostgreSQL URL Shortener.

No real Postgres connection is used anywhere in this file: asyncpg's pool
is replaced with lightweight fakes, and startup/shutdown DB calls in the
FastAPI lifespan are monkeypatched out. This makes the whole suite fast and
fully offline.

Run with:
    pip install pytest pytest-asyncio httpx
    pytest test_url_shortener.py -v
"""
import asyncio
import string
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app import cache as cache_module
from app import main
from app.config import Settings, get_settings
from app.utils import ALPHABET, generate_short_code, is_valid_custom_code


# ---------------------------------------------------------------------------
# Fakes standing in for asyncpg, so no real Postgres is ever touched
# ---------------------------------------------------------------------------

class _NoopAsyncCM:
    """Stands in for `conn.transaction()`'s async context manager."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class _AcquireCM:
    """Stands in for `pool.acquire()`'s async context manager."""

    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc_info):
        return False


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _AcquireCM(self.conn)


def make_fake_conn() -> AsyncMock:
    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=_NoopAsyncCM())
    return conn


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_conn():
    return make_fake_conn()


@pytest.fixture
def client(monkeypatch, fake_conn):
    """A TestClient wired to a fake Postgres pool; startup/shutdown DB
    calls are stubbed so no real network I/O happens."""
    fake_pool = FakePool(fake_conn)

    monkeypatch.setattr(main, "init_pool", AsyncMock())
    monkeypatch.setattr(main, "start_cache_listener", AsyncMock())
    monkeypatch.setattr(main, "close_pool", AsyncMock())
    monkeypatch.setattr(main, "stop_cache_listener", AsyncMock())
    monkeypatch.setattr(main, "get_pool", lambda: fake_pool)

    # Fresh cache and hit-buffer for every test
    cache_module._cache = None
    main._hit_buffer.clear()

    with TestClient(main.app) as test_client:
        yield test_client, fake_conn


# ---------------------------------------------------------------------------
# utils.py
# ---------------------------------------------------------------------------

class TestGenerateShortCode:
    def test_default_length(self):
        code = generate_short_code(7)
        assert len(code) == 7

    def test_custom_length(self):
        code = generate_short_code(12)
        assert len(code) == 12

    def test_uses_only_expected_alphabet(self):
        code = generate_short_code(500)
        assert set(code) <= set(ALPHABET)
        assert set(ALPHABET) == set(string.ascii_letters + string.digits)

    def test_codes_are_randomized(self):
        codes = {generate_short_code(10) for _ in range(50)}
        # Astronomically unlikely to collide if randomness is working
        assert len(codes) == 50


class TestIsValidCustomCode:
    @pytest.mark.parametrize(
        "code",
        ["abc", "My-Code_123", "a" * 32, "x_y-z"],
    )
    def test_valid_codes(self, code):
        assert is_valid_custom_code(code) is True

    @pytest.mark.parametrize(
        "code",
        [
            "ab",          # too short
            "a" * 33,      # too long
            "bad code",    # contains a space
            "bad!code",    # contains an invalid symbol
            "",            # empty
        ],
    )
    def test_invalid_codes(self, code):
        assert is_valid_custom_code(code) is False


# ---------------------------------------------------------------------------
# cache.py -- LRUCache
# ---------------------------------------------------------------------------

class TestLRUCache:
    @pytest.mark.asyncio
    async def test_set_and_get(self):
        c = cache_module.LRUCache(max_size=10)
        await c.set("abc", {"long_url": "https://example.com"})
        result = await c.get("abc")
        assert result == {"long_url": "https://example.com"}

    @pytest.mark.asyncio
    async def test_get_missing_key_returns_none(self):
        c = cache_module.LRUCache(max_size=10)
        assert await c.get("missing") is None

    @pytest.mark.asyncio
    async def test_invalidate_removes_entry(self):
        c = cache_module.LRUCache(max_size=10)
        await c.set("abc", {"long_url": "https://example.com"})
        await c.invalidate("abc")
        assert await c.get("abc") is None

    @pytest.mark.asyncio
    async def test_invalidate_missing_key_is_a_noop(self):
        c = cache_module.LRUCache(max_size=10)
        await c.invalidate("does-not-exist")  # should not raise

    @pytest.mark.asyncio
    async def test_clear_empties_cache(self):
        c = cache_module.LRUCache(max_size=10)
        await c.set("a", {"long_url": "1"})
        await c.set("b", {"long_url": "2"})
        await c.clear()
        assert c.size() == 0

    @pytest.mark.asyncio
    async def test_evicts_least_recently_used_when_full(self):
        c = cache_module.LRUCache(max_size=2)
        await c.set("a", {"long_url": "1"})
        await c.set("b", {"long_url": "2"})
        await c.set("c", {"long_url": "3"})  # should evict "a"
        assert await c.get("a") is None
        assert await c.get("b") == {"long_url": "2"}
        assert await c.get("c") == {"long_url": "3"}
        assert c.size() == 2

    @pytest.mark.asyncio
    async def test_get_refreshes_recency(self):
        c = cache_module.LRUCache(max_size=2)
        await c.set("a", {"long_url": "1"})
        await c.set("b", {"long_url": "2"})
        await c.get("a")               # "a" is now most-recently-used
        await c.set("c", {"long_url": "3"})  # should evict "b", not "a"
        assert await c.get("b") is None
        assert await c.get("a") == {"long_url": "1"}
        assert await c.get("c") == {"long_url": "3"}


# ---------------------------------------------------------------------------
# config.py
# ---------------------------------------------------------------------------

class TestSettings:
    def test_defaults(self):
        s = Settings(_env_file=None)
        assert s.base_url == "http://localhost:8000"
        assert s.short_code_length == 7
        assert s.unlogged_table is True
        assert s.default_expiry_days is None

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("URLSHORT_BASE_URL", "https://short.example.com")
        monkeypatch.setenv("URLSHORT_SHORT_CODE_LENGTH", "10")
        monkeypatch.setenv("URLSHORT_UNLOGGED_TABLE", "false")
        get_settings.cache_clear()
        try:
            s = get_settings()
            assert s.base_url == "https://short.example.com"
            assert s.short_code_length == 10
            assert s.unlogged_table is False
        finally:
            get_settings.cache_clear()


# ---------------------------------------------------------------------------
# main.py -- API endpoints
# ---------------------------------------------------------------------------

class TestHealthEndpoint:
    def test_health_ok(self, client):
        test_client, fake_conn = client
        response = test_client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert "cache_size" in body


class TestShortenEndpoint:
    def test_shorten_generates_random_code(self, client):
        test_client, fake_conn = client
        fake_conn.execute.return_value = "INSERT 0 1"

        response = test_client.post(
            "/api/shorten", json={"url": "https://example.com/very/long/path"}
        )

        assert response.status_code == 201
        body = response.json()
        assert body["long_url"] == "https://example.com/very/long/path"
        assert len(body["short_code"]) == get_settings().short_code_length
        assert body["short_url"].endswith(body["short_code"])
        assert body["expires_at"] is None

    def test_shorten_with_custom_code(self, client):
        test_client, fake_conn = client
        fake_conn.execute.return_value = "INSERT 0 1"

        response = test_client.post(
            "/api/shorten",
            json={"url": "https://example.com", "custom_code": "my-code"},
        )

        assert response.status_code == 201
        assert response.json()["short_code"] == "my-code"

    def test_shorten_with_invalid_custom_code_format(self, client):
        test_client, fake_conn = client

        response = test_client.post(
            "/api/shorten",
            json={"url": "https://example.com", "custom_code": "bad code!"},
        )

        assert response.status_code == 400

    def test_shorten_with_duplicate_custom_code(self, client):
        test_client, fake_conn = client
        fake_conn.execute.side_effect = asyncpg.UniqueViolationError()

        response = test_client.post(
            "/api/shorten",
            json={"url": "https://example.com", "custom_code": "taken"},
        )

        assert response.status_code == 409

    def test_shorten_with_expiry(self, client):
        test_client, fake_conn = client
        fake_conn.execute.return_value = "INSERT 0 1"

        response = test_client.post(
            "/api/shorten",
            json={"url": "https://example.com", "expires_in_days": 30},
        )

        assert response.status_code == 201
        expires_at = datetime.fromisoformat(response.json()["expires_at"])
        expected = datetime.now(timezone.utc) + timedelta(days=30)
        assert abs((expires_at - expected).total_seconds()) < 60

    def test_shorten_rejects_invalid_url(self, client):
        test_client, fake_conn = client

        response = test_client.post("/api/shorten", json={"url": "not-a-url"})

        assert response.status_code == 422


class TestResolveEndpoint:
    def test_resolve_not_found(self, client):
        test_client, fake_conn = client
        fake_conn.fetchrow.return_value = None

        response = test_client.get("/doesnotexist", follow_redirects=False)

        assert response.status_code == 404

    def test_resolve_success_redirects(self, client):
        test_client, fake_conn = client
        fake_conn.fetchrow.return_value = {
            "long_url": "https://example.com/target",
            "expires_at": None,
        }

        response = test_client.get("/abc1234", follow_redirects=False)

        assert response.status_code == 307
        assert response.headers["location"] == "https://example.com/target"

    def test_resolve_expired_link(self, client):
        test_client, fake_conn = client
        fake_conn.fetchrow.return_value = {
            "long_url": "https://example.com/target",
            "expires_at": datetime.now(timezone.utc) - timedelta(days=1),
        }

        response = test_client.get("/expiredcode", follow_redirects=False)

        assert response.status_code == 410

    def test_resolve_second_hit_served_from_cache_not_db(self, client):
        test_client, fake_conn = client
        fake_conn.fetchrow.return_value = {
            "long_url": "https://example.com/target",
            "expires_at": None,
        }

        first = test_client.get("/cached1", follow_redirects=False)
        second = test_client.get("/cached1", follow_redirects=False)

        assert first.status_code == 307
        assert second.status_code == 307
        # Only the first request should have hit the "database"
        assert fake_conn.fetchrow.call_count == 1


class TestStatsEndpoint:
    def test_stats_found(self, client):
        test_client, fake_conn = client
        now = datetime.now(timezone.utc)
        fake_conn.fetchrow.return_value = {
            "short_code": "abc1234",
            "long_url": "https://example.com/target",
            "created_at": now,
            "expires_at": None,
            "hits": 42,
            "last_accessed_at": now,
        }

        response = test_client.get("/api/stats/abc1234")

        assert response.status_code == 200
        assert response.json()["hits"] == 42

    def test_stats_not_found(self, client):
        test_client, fake_conn = client
        fake_conn.fetchrow.return_value = None

        response = test_client.get("/api/stats/missing")

        assert response.status_code == 404


class TestDeleteEndpoint:
    def test_delete_existing_code(self, client):
        test_client, fake_conn = client
        fake_conn.execute.return_value = "DELETE 1"

        response = test_client.delete("/api/abc1234")

        assert response.status_code == 204

    def test_delete_missing_code(self, client):
        test_client, fake_conn = client
        fake_conn.execute.return_value = "DELETE 0"

        response = test_client.delete("/api/missing")

        assert response.status_code == 404


# ---------------------------------------------------------------------------
# main.py -- buffered hit counting
# ---------------------------------------------------------------------------

class TestHitFlushing:
    @pytest.mark.asyncio
    async def test_flush_hits_batches_and_clears_buffer(self, monkeypatch):
        fake_conn = make_fake_conn()
        fake_pool = FakePool(fake_conn)
        monkeypatch.setattr(main, "get_pool", lambda: fake_pool)

        main._hit_buffer.clear()
        main._hit_buffer["code1"] = 3
        main._hit_buffer["code2"] = 1

        await main._flush_hits()

        assert main._hit_buffer == {}
        assert fake_conn.execute.call_count == 2
        called_codes = {call.args[2] for call in fake_conn.execute.call_args_list}
        assert called_codes == {"code1", "code2"}

    @pytest.mark.asyncio
    async def test_flush_hits_is_a_noop_when_buffer_empty(self, monkeypatch):
        fake_conn = make_fake_conn()
        fake_pool = FakePool(fake_conn)
        monkeypatch.setattr(main, "get_pool", lambda: fake_pool)

        main._hit_buffer.clear()
        await main._flush_hits()

        fake_conn.execute.assert_not_called()


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
