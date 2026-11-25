"""Redis cache (tenant-prefixed). Fails silently so the app keeps working offline."""
import json
import redis


class RedisCache:
    def __init__(self, url: str = "", ttl: int = 60, prefix: str = "", client=None):
        self._ttl, self._p = ttl, prefix
        self._r = client or redis.Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1,
                                                 decode_responses=True)

    def scoped(self, prefix: str) -> "RedisCache":
        """Cheap view sharing the same Redis client but with its own key namespace."""
        return RedisCache(ttl=self._ttl, prefix=prefix, client=self._r)

    def _k(self, key: str) -> str:
        return f"{self._p}:{key}" if self._p else key

    def get(self, key: str):
        try:
            raw = self._r.get(self._k(key))
            return json.loads(raw) if raw else None
        except redis.RedisError:
            return None

    def set(self, key: str, value, ttl: int | None = None) -> None:
        try:
            self._r.setex(self._k(key), ttl or self._ttl, json.dumps(value, default=str))
        except redis.RedisError:
            pass

    def invalidate(self, *prefixes: str) -> None:
        try:
            for p in prefixes:
                for k in self._r.scan_iter(f"{self._k(p)}*", count=500):
                    self._r.delete(k)
        except redis.RedisError:
            pass

    def incr_window(self, key: str, ttl: int) -> int | None:
        """Atomic counter with expiry (rate limiting). None if Redis is unavailable."""
        try:
            k = self._k(key)
            pipe = self._r.pipeline()
            pipe.incr(k)
            pipe.expire(k, ttl, nx=True)
            return int(pipe.execute()[0])
        except redis.RedisError:
            return None

    def alive(self) -> bool:
        try:
            return bool(self._r.ping())
        except redis.RedisError:
            return False
