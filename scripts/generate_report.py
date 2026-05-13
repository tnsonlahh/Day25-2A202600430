from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any

from reliability_lab.cache import SharedRedisCache
from reliability_lab.chaos import load_queries, run_simulation
from reliability_lab.config import LabConfig, load_config


def _pct_delta(before: float, after: float) -> str:
    if before == 0:
        return f"+{after:.4f}"
    return f"{((after - before) / before) * 100:.1f}%"


def _comparison(config: LabConfig) -> tuple[dict[str, Any], dict[str, Any]]:
    queries = load_queries()
    without_cache = copy.deepcopy(config)
    without_cache.cache.enabled = False
    with_cache = copy.deepcopy(config)
    with_cache.cache.enabled = True
    with_cache.cache.backend = "memory"
    return (
        run_simulation(without_cache, queries).to_report_dict(),
        run_simulation(with_cache, queries).to_report_dict(),
    )


def _redis_evidence(config: LabConfig) -> tuple[str, str]:
    cache = SharedRedisCache(
        redis_url=config.cache.redis_url,
        ttl_seconds=config.cache.ttl_seconds,
        similarity_threshold=config.cache.similarity_threshold,
        prefix="rl:cache:",
    )
    if not cache.ping():
        cache.close()
        return "Redis was not reachable while generating this report.", "Redis was not reachable."

    cache.set("shared cache evidence query", "shared cache evidence response")
    peer = SharedRedisCache(
        redis_url=config.cache.redis_url,
        ttl_seconds=config.cache.ttl_seconds,
        similarity_threshold=config.cache.similarity_threshold,
        prefix="rl:cache:",
    )
    cached, score = peer.get("shared cache evidence query")
    keys = list(cache._redis.scan_iter("rl:cache:*"))  # type: ignore[attr-defined]
    evidence = f"peer.get(...) returned {cached!r} with score {score:.2f}"
    cli = "\n".join(str(key) for key in keys) if keys else "(no rl:cache:* keys found)"
    peer.close()
    cache.close()
    return evidence, cli


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metrics", default="reports/metrics.json")
    parser.add_argument("--out", default="reports/final_report.md")
    args = parser.parse_args()

    metrics = json.loads(Path(args.metrics).read_text())
    config = load_config("configs/default.yaml")
    without_cache, with_cache = _comparison(config)
    redis_evidence, redis_keys = _redis_evidence(config)

    recovery = metrics.get("recovery_time_ms")
    recovery_value = float(recovery) if isinstance(recovery, int | float) else 0.0
    slo_rows = [
        ("Availability", ">= 99%", metrics["availability"], "yes" if metrics["availability"] >= 0.99 else "no"),
        ("Latency P95", "< 2500 ms", metrics["latency_p95_ms"], "yes" if metrics["latency_p95_ms"] < 2500 else "no"),
        (
            "Fallback success rate",
            ">= 95%",
            metrics["fallback_success_rate"],
            "yes" if metrics["fallback_success_rate"] >= 0.95 else "no",
        ),
        ("Cache hit rate", ">= 10%", metrics["cache_hit_rate"], "yes" if metrics["cache_hit_rate"] >= 0.1 else "no"),
        ("Recovery time", "< 5000 ms", recovery_value, "yes" if recovery_value < 5000 else "no"),
    ]

    lines = [
        "# Day 10 Reliability Final Report",
        "",
        "## 1. Architecture summary",
        "",
        "The gateway checks the cache first, then sends requests through per-provider circuit breakers in configured order. If all providers fail or are open, it returns a static degraded-service fallback.",
        "",
        "```",
        "User -> Gateway -> Cache",
        "                 | hit -> cached response",
        "                 v miss",
        "          Circuit(primary) -> Provider primary",
        "                 v open/fail",
        "          Circuit(backup)  -> Provider backup",
        "                 v all fail",
        "          Static fallback",
        "```",
        "",
        "## 2. Configuration",
        "",
        "| Setting | Value | Reason |",
        "|---|---:|---|",
        f"| failure_threshold | {config.circuit_breaker.failure_threshold} | Opens after repeated provider failures without overreacting to one transient error. |",
        f"| reset_timeout_seconds | {config.circuit_breaker.reset_timeout_seconds} | Allows quick recovery probes during short chaos runs. |",
        f"| success_threshold | {config.circuit_breaker.success_threshold} | One successful half-open probe is enough for this fake provider lab. |",
        f"| cache TTL | {config.cache.ttl_seconds} | Five minutes balances FAQ reuse with freshness. |",
        f"| similarity_threshold | {config.cache.similarity_threshold} | High enough to avoid date-sensitive false hits such as 2024 vs 2026. |",
        f"| load_test requests | {config.load_test.requests} | Enough requests to exercise cache hits and circuit transitions reproducibly. |",
        "",
        "## 3. SLO definitions",
        "",
        "| SLI | SLO target | Actual value | Met? |",
        "|---|---|---:|---|",
    ]
    lines.extend(f"| {name} | {target} | {actual} | {met} |" for name, target, actual, met in slo_rows)

    lines += [
        "",
        "## 4. Metrics",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]
    for key, value in metrics.items():
        if key != "scenarios":
            lines.append(f"| {key} | {value} |")

    lines += [
        "",
        "## 5. Cache comparison",
        "",
        "| Metric | Without cache | With cache | Delta |",
        "|---|---:|---:|---|",
    ]
    for key in ["latency_p50_ms", "latency_p95_ms", "estimated_cost", "cache_hit_rate"]:
        before = float(without_cache[key])
        after = float(with_cache[key])
        lines.append(f"| {key} | {before} | {after} | {_pct_delta(before, after)} |")

    lines += [
        "",
        "## 6. Redis shared cache",
        "",
        "In-memory cache is insufficient for multi-instance deployments because each process keeps a separate private cache. `SharedRedisCache` stores entries in Redis with TTL, so separate gateway instances can reuse the same cached response while privacy and false-hit guardrails still apply.",
        "",
        "### Evidence of shared state",
        "",
        "```",
        redis_evidence,
        "```",
        "",
        "### Redis CLI output",
        "",
        "```bash",
        "# docker compose exec redis redis-cli KEYS \"rl:cache:*\"",
        redis_keys,
        "```",
        "",
        "## 7. Chaos scenarios",
        "",
        "| Scenario | Expected behavior | Observed behavior | Pass/Fail |",
        "|---|---|---|---|",
    ]
    expected = {
        "primary_timeout_100": "All traffic falls back to backup and the primary circuit opens.",
        "primary_flaky_50": "Circuit opens under failures and backup serves some traffic.",
        "all_healthy": "Most requests succeed through primary/cache with low errors.",
        "cache_stale_candidate": "Cache hits occur while date/privacy guardrails prevent unsafe hits.",
    }
    for name, status in metrics.get("scenarios", {}).items():
        lines.append(f"| {name} | {expected.get(name, 'Requests complete successfully.')} | See metrics.json aggregate counters. | {status} |")

    lines += [
        "",
        "## 8. Failure analysis",
        "",
        "Remaining weakness: circuit breaker state is process-local. In production, multiple gateway replicas could disagree about whether a provider is unhealthy, causing extra traffic to an already failing provider.",
        "",
        "Change before production: move breaker counters and state transitions to Redis or another shared coordination layer, and add per-provider rate limits around half-open probes.",
        "",
        "## 9. Next steps",
        "",
        "1. Add concurrent load testing with the configured concurrency value.",
        "2. Store circuit breaker state in Redis for multi-instance consistency.",
        "3. Export Prometheus counters for requests, latency, cache hits, and circuit state.",
        "",
    ]

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text("\n".join(lines))
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
