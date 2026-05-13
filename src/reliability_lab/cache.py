from __future__ import annotations

import hashlib
import math
import re
import time
from dataclasses import dataclass
from collections import Counter
from typing import Any

# ---------------------------------------------------------------------------
# Shared utilities — use these in both ResponseCache and SharedRedisCache
# ---------------------------------------------------------------------------

PRIVACY_PATTERNS = re.compile(
    r"\b(balance|password|credit.card|ssn|social.security|user.\d+|account.\d+)\b",
    re.IGNORECASE,
)


def _is_uncacheable(query: str) -> bool:
    """Return True if query contains privacy-sensitive keywords."""
    return bool(PRIVACY_PATTERNS.search(query))


def _looks_like_false_hit(query: str, cached_key: str) -> bool:
    """Return True if query and cached key contain different 4-digit numbers (years, IDs)."""
    nums_q = set(re.findall(r"\b\d{4}\b", query))
    nums_c = set(re.findall(r"\b\d{4}\b", cached_key))
    return bool(nums_q and nums_c and nums_q != nums_c)


# ---------------------------------------------------------------------------
# In-memory cache (existing)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CacheEntry:
    key: str
    value: str
    created_at: float
    metadata: dict[str, str]


class ResponseCache:
    """Simple in-memory cache with similarity lookup and safety guardrails."""

    def __init__(self, ttl_seconds: int, similarity_threshold: float):
        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self._entries: list[CacheEntry] = []
        self.false_hit_log: list[dict[str, object]] = []

    def get(self, query: str) -> tuple[str | None, float]:
        if _is_uncacheable(query):
            return None, 0.0
        best_value: str | None = None
        best_key: str | None = None
        best_score = 0.0
        now = time.time()
        self._entries = [e for e in self._entries if now - e.created_at <= self.ttl_seconds]
        for entry in self._entries:
            if query.strip().lower() == entry.key.strip().lower():
                return entry.value, 1.0
            if _looks_like_false_hit(query, entry.key):
                self.false_hit_log.append({"query": query, "cached_key": entry.key, "score": 0.0})
                continue
            score = self.similarity(query, entry.key)
            if score > best_score:
                best_score = score
                best_value = entry.value
                best_key = entry.key
        if best_score >= self.similarity_threshold:
            if best_key is not None and _looks_like_false_hit(query, best_key):
                self.false_hit_log.append(
                    {"query": query, "cached_key": best_key, "score": round(best_score, 4)}
                )
                return None, best_score
            return best_value, best_score
        return None, best_score

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        if _is_uncacheable(query):
            return
        self._entries.append(CacheEntry(query, value, time.time(), metadata or {}))

    @staticmethod
    def similarity(a: str, b: str) -> float:
        """Deterministic semantic-style similarity with token and character signals."""
        norm_a = a.strip().lower()
        norm_b = b.strip().lower()
        if not norm_a or not norm_b:
            return 0.0
        if norm_a == norm_b:
            return 1.0
        if _looks_like_false_hit(norm_a, norm_b):
            return 0.0

        token_re = re.compile(r"[a-z0-9]+")
        tokens_a = token_re.findall(norm_a)
        tokens_b = token_re.findall(norm_b)
        if not tokens_a or not tokens_b:
            return 0.0

        token_left = set(tokens_a)
        token_right = set(tokens_b)
        token_score = len(token_left & token_right) / len(token_left | token_right)

        def char_ngrams(text: str, n: int = 3) -> Counter[str]:
            compact = re.sub(r"\s+", " ", text)
            if len(compact) < n:
                return Counter({compact: 1})
            return Counter(compact[i : i + n] for i in range(len(compact) - n + 1))

        grams_a = char_ngrams(norm_a)
        grams_b = char_ngrams(norm_b)
        dot = sum(count * grams_b[gram] for gram, count in grams_a.items())
        mag_a = math.sqrt(sum(count * count for count in grams_a.values()))
        mag_b = math.sqrt(sum(count * count for count in grams_b.values()))
        char_score = dot / (mag_a * mag_b) if mag_a and mag_b else 0.0
        return (0.65 * token_score) + (0.35 * char_score)


# ---------------------------------------------------------------------------
# Redis shared cache (new)
# ---------------------------------------------------------------------------


class SharedRedisCache:
    """Redis-backed shared cache for multi-instance deployments.

    Data model (suggested):
        Key    = "{prefix}{query_hash}"   (Redis String namespace)
        Value  = Redis Hash with fields:  "query", "response"
        TTL    = Redis EXPIRE (automatic cleanup — no manual eviction)

    For similarity lookup: SCAN all keys with self.prefix, HGET each entry's
    "query" field, compute similarity locally via ResponseCache.similarity().

    Provided helpers:
        _is_uncacheable(query)          — True if privacy-sensitive
        _looks_like_false_hit(q, key)   — True if 4-digit numbers differ
        self._query_hash(query)         — deterministic short hash for Redis key
        ResponseCache.similarity(a, b)  — reuse your improved similarity function
    """

    def __init__(
        self,
        redis_url: str,
        ttl_seconds: int,
        similarity_threshold: float,
        prefix: str = "rl:cache:",
    ):
        import redis as redis_lib

        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self.prefix = prefix
        self.false_hit_log: list[dict[str, object]] = []
        self._redis: Any = redis_lib.Redis.from_url(redis_url, decode_responses=True)

    def ping(self) -> bool:
        """Check Redis connectivity."""
        try:
            return bool(self._redis.ping())
        except Exception:
            return False

    def get(self, query: str) -> tuple[str | None, float]:
        """Look up a cached response from Redis.

        1. Return (None, 0.0) if _is_uncacheable(query)
        2. Build exact-match key: f"{self.prefix}{self._query_hash(query)}"
        3. Try self._redis.hget(key, "response") — if found return (response, 1.0)
        4. Otherwise self._redis.scan_iter(f"{self.prefix}*") to iterate all cached keys
        5. For each key, HGET "query" field and compute
           ResponseCache.similarity(query, cached_query)
        6. Track best match that is >= self.similarity_threshold
        7. Before returning a match, check _looks_like_false_hit(); if true,
           append to self.false_hit_log and return (None, best_score)
        """
        if _is_uncacheable(query):
            return None, 0.0
        try:
            exact_key = f"{self.prefix}{self._query_hash(query)}"
            exact = self._redis.hget(exact_key, "response")
            if exact is not None:
                return str(exact), 1.0

            best_response: str | None = None
            best_query: str | None = None
            best_score = 0.0
            for key in self._redis.scan_iter(f"{self.prefix}*"):
                cached_query = self._redis.hget(key, "query")
                if cached_query is None:
                    continue
                if _looks_like_false_hit(query, str(cached_query)):
                    self.false_hit_log.append(
                        {"query": query, "cached_key": str(cached_query), "score": 0.0}
                    )
                    continue
                score = ResponseCache.similarity(query, str(cached_query))
                if score > best_score:
                    best_score = score
                    best_query = str(cached_query)
                    response = self._redis.hget(key, "response")
                    best_response = str(response) if response is not None else None

            if best_response is not None and best_score >= self.similarity_threshold:
                if best_query is not None and _looks_like_false_hit(query, best_query):
                    self.false_hit_log.append(
                        {"query": query, "cached_key": best_query, "score": round(best_score, 4)}
                    )
                    return None, best_score
                return best_response, best_score
            return None, best_score
        except Exception:
            return None, 0.0

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        """Store a response in Redis with TTL.

        1. Return immediately if _is_uncacheable(query)
        2. Build key: f"{self.prefix}{self._query_hash(query)}"
        3. self._redis.hset(key, mapping={"query": query, "response": value})
        4. self._redis.expire(key, self.ttl_seconds)
        """
        if _is_uncacheable(query):
            return
        try:
            key = f"{self.prefix}{self._query_hash(query)}"
            mapping = {"query": query, "response": value}
            if metadata:
                mapping.update({f"meta:{k}": v for k, v in metadata.items()})
            self._redis.hset(key, mapping=mapping)
            self._redis.expire(key, self.ttl_seconds)
        except Exception:
            return

    def flush(self) -> None:
        """Remove all entries with this cache prefix (for testing)."""
        for key in self._redis.scan_iter(f"{self.prefix}*"):
            self._redis.delete(key)

    def close(self) -> None:
        """Close Redis connection."""
        if self._redis is not None:
            self._redis.close()

    @staticmethod
    def _query_hash(query: str) -> str:
        """Deterministic short hash for a query string."""
        return hashlib.md5(query.lower().strip().encode()).hexdigest()[:12]
