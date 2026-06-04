import asyncio
import time
from typing import Any


class AsyncTTLCache:
    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._lock = asyncio.Lock()

    async def _get_key_lock(self, key: str) -> asyncio.Lock:
        async with self._lock:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock

    async def get(self, key: str) -> Any | None:
        now = time.monotonic()
        async with self._lock:
            cached = self._cache.get(key)
            if cached is None:
                return None

            expires_at, value = cached
            if expires_at <= now:
                self._cache.pop(key, None)
                self._locks.pop(key, None)
                return None

            return value

    async def set(self, key: str, value: Any, ttl_seconds: float) -> Any:
        expires_at = time.monotonic() + ttl_seconds
        async with self._lock:
            self._cache[key] = (expires_at, value)
        return value

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._cache.pop(key, None)
            self._locks.pop(key, None)

    async def get_or_set(self, key: str, ttl_seconds: float, factory) -> Any:
        cached = await self.get(key)
        if cached is not None:
            return cached

        key_lock = await self._get_key_lock(key)
        async with key_lock:
            cached = await self.get(key)
            if cached is not None:
                return cached

            value = await factory()
            await self.set(key, value, ttl_seconds)
            return value

    async def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        async with self._lock:
            expired_keys = [
                key for key, (expires_at, _) in self._cache.items()
                if expires_at <= now
            ]
            for key in expired_keys:
                self._cache.pop(key, None)
                self._locks.pop(key, None)

            return {
                key: value
                for key, (_, value) in self._cache.items()
            }
