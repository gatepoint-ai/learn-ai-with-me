"""Valkey-backed query cache.

Two properties matter here. First, a cache miss must never be fatal -- if Valkey
is down the platform degrades to uncached queries rather than erroring, so every
operation swallows connection failures. Second, the key must change whenever
anything that affects the result changes, which is why the lens's own
definition is hashed into it.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import redis

from ..settings import ValkeySettings


def cache_key(lens_name: str, lens_fingerprint: str, payload: dict[str, Any]) -> str:
    """Build a stable cache key.

    `lens_fingerprint` folds the lens definition into the key, so editing a
    lens's SQL invalidates its cached results without a manual flush -- the
    failure mode that otherwise serves yesterday's schema for half an hour.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(
        f"{lens_fingerprint}\x00{canonical}".encode()
    ).hexdigest()
    return f"prism:{lens_name}:{digest}"


def fingerprint(*parts: str) -> str:
    """Short stable hash of the pieces that define a lens's shape."""
    joined = "\x00".join(parts)
    return hashlib.sha256(joined.encode()).hexdigest()[:16]


class CacheClient:
    def __init__(self, settings: ValkeySettings) -> None:
        self._settings = settings
        self._client: redis.Redis | None = None
        self.hits = 0
        self.misses = 0

    @property
    def enabled(self) -> bool:
        return self._settings.cache_enabled

    def _connect(self) -> redis.Redis | None:
        if not self.enabled:
            return None
        if self._client is None:
            self._client = redis.Redis(
                host=self._settings.valkey_host,
                port=self._settings.valkey_port,
                db=self._settings.valkey_db,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
        return self._client

    def get(self, key: str) -> Any | None:
        client = self._connect()
        if client is None:
            return None
        try:
            raw = client.get(key)
        except redis.RedisError:
            # Degrade to uncached rather than failing the request.
            return None
        if raw is None:
            self.misses += 1
            return None
        self.hits += 1
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def set(self, key: str, value: Any, ttl: int) -> None:
        client = self._connect()
        if client is None or ttl <= 0:
            return
        try:
            client.setex(
                key, ttl, json.dumps(value, separators=(",", ":"), default=str)
            )
        except redis.RedisError:
            pass

    def delete_prefix(self, prefix: str) -> int:
        """Drop every key under a prefix. Used by `make flush-cache`."""
        client = self._connect()
        if client is None:
            return 0
        removed = 0
        try:
            for key in client.scan_iter(match=f"{prefix}*", count=500):
                removed += client.delete(key)
        except redis.RedisError:
            return removed
        return removed

    def ping(self) -> bool:
        client = self._connect()
        if client is None:
            return False
        try:
            return bool(client.ping())
        except redis.RedisError:
            return False

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except redis.RedisError:
                pass
            self._client = None
