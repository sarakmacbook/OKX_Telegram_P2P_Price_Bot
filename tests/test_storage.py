"""State backends."""

import json

import pytest

import storage


# ── storage ────────────────────────────────────────────────────────────────
def test_file_store_roundtrip(tmp_path):
    store = storage.FileStore(tmp_path / "data.json")
    assert store.load() is None
    store.save({"group": -100123, "settings": {"price_links": True}})
    assert store.load()["group"] == -100123
    assert json.loads((tmp_path / "data.json").read_text())["group"] == -100123


def test_file_store_survives_broken_json(tmp_path):
    p = tmp_path / "data.json"
    p.write_text("{not json")
    assert storage.FileStore(p).load() is None


def test_file_store_never_raises_on_unwritable_path(tmp_path):
    store = storage.FileStore(tmp_path / "data.json")
    store.path = tmp_path / "missing" / "\0bad" / "data.json"     # cannot be created
    store.save({"a": 1})                                          # must not raise


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_redis_store_uses_rest_commands(monkeypatch):
    calls = []

    def fake_post(url, json=None, timeout=None, headers=None):
        calls.append((url, json, headers))
        if json[0] == "GET":
            return _FakeResponse({"result": '{"group": -42}'})
        return _FakeResponse({"result": "OK"})

    monkeypatch.setattr("httpx.post", fake_post, raising=True)
    store = storage.RedisStore("https://redis.example", "tok", key="k")

    assert store.load() == {"group": -42}
    store.save({"group": -42})
    assert calls[0][1] == ["GET", "k"]
    assert calls[1][1][0] == "SET" and calls[1][1][1] == "k"
    assert calls[1][2]["Authorization"] == "Bearer tok"
    assert "redis.example" in store.describe()


def test_redis_store_swallows_errors(monkeypatch):
    def boom(*a, **k):
        raise OSError("no network")

    monkeypatch.setattr("httpx.post", boom, raising=True)
    store = storage.RedisStore("https://redis.example", "tok")
    assert store.load() is None
    store.save({"x": 1})                                          # must not raise


def test_redis_is_preferred_over_file(tmp_path, monkeypatch):
    monkeypatch.setenv("KV_REST_API_URL", "https://kv.example")
    monkeypatch.setenv("KV_REST_API_TOKEN", "tok")
    store = storage.build_store(tmp_path)
    assert store.backend == "redis"
    assert store.key == storage.DEFAULT_KEY


def test_readonly_dir_falls_back_to_tmp(tmp_path, monkeypatch):
    monkeypatch.delenv("P2P_DATA_DIR", raising=False)
    readonly = tmp_path / "ro"
    store = storage.build_store(readonly)
    if store.backend == "file" and store.path.parent == readonly:
        pytest.skip("directory is writable in this environment")
    assert store.backend in ("file", "none")


def test_file_selection_overrides_available_redis(tmp_path, monkeypatch):
    """An explicit local database choice must beat auto-detected KV credentials."""
    monkeypatch.setenv("P2P_STATE_BACKEND", "file")
    monkeypatch.setenv("P2P_STATE_FILE", str(tmp_path / "data.json"))
    monkeypatch.setenv("KV_REST_API_URL", "https://kv.example")
    monkeypatch.setenv("KV_REST_API_TOKEN", "tok")

    store = storage.build_store(tmp_path)

    assert store.backend == "file"
    assert store.path == tmp_path / "data.json"


def test_explicit_redis_never_silently_falls_back_to_file(tmp_path, monkeypatch):
    """A typo/outage in a selected shared database must not split bot state."""
    monkeypatch.setenv("P2P_STATE_BACKEND", "redis")
    monkeypatch.delenv("KV_REST_API_URL", raising=False)
    monkeypatch.delenv("KV_REST_API_TOKEN", raising=False)

    store = storage.build_store(tmp_path)

    assert store.backend == "none"
    assert "Redis was selected" in store.describe()


def test_backend_aliases_are_normalized(monkeypatch):
    monkeypatch.setenv("P2P_STORAGE_BACKEND", "kv")
    monkeypatch.delenv("P2P_STATE_BACKEND", raising=False)
    assert storage.state_backend() == "redis"
    assert storage.normalize_backend("json") == "file"
    assert storage.normalize_backend("postgres") is None


# ── the "connect a database" link ──────────────────────────────────────────
def test_database_link_defaults_to_the_vercel_storage_page(monkeypatch):
    monkeypatch.delenv(storage.DATABASE_LINK_ENV, raising=False)
    assert storage.database_link() == "https://vercel.com/dashboard/stores"


def test_database_link_can_point_at_your_own_store(monkeypatch):
    monkeypatch.setenv(storage.DATABASE_LINK_ENV, "https://console.upstash.com/redis/123")
    assert storage.database_link() == "https://console.upstash.com/redis/123"


def test_database_link_ignores_a_value_that_is_not_a_url(monkeypatch):
    """A typo in the environment must not turn the button into a broken link."""
    monkeypatch.setenv(storage.DATABASE_LINK_ENV, "vercel.com/dashboard/stores")
    assert storage.database_link() == storage.DEFAULT_DATABASE_LINK
    monkeypatch.setenv(storage.DATABASE_LINK_ENV, "https://")
    assert storage.database_link() == storage.DEFAULT_DATABASE_LINK


def test_database_connected_follows_the_credentials(monkeypatch):
    for url_var, token_var in storage.REDIS_ENV_PAIRS:
        monkeypatch.delenv(url_var, raising=False)
        monkeypatch.delenv(token_var, raising=False)
    assert storage.database_connected() is False

    monkeypatch.setenv("KV_REST_API_URL", "https://kv.example")
    monkeypatch.setenv("KV_REST_API_TOKEN", "tok")
    assert storage.database_connected() is True

    # With a store in hand the verified answer wins over the environment.
    assert storage.database_connected(storage.RedisStore("https://kv.example", "tok")) is True
    assert storage.database_connected(storage.FileStore("data.json")) is False
    assert storage.database_connected(storage.NullStore()) is False
