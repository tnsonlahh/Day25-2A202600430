# Day 10 Reliability Final Report

## 1. Architecture summary

The gateway checks the cache first, then sends requests through per-provider circuit breakers in configured order. If all providers fail or are open, it returns a static degraded-service fallback.

```
User -> Gateway -> Cache
                 | hit -> cached response
                 v miss
          Circuit(primary) -> Provider primary
                 v open/fail
          Circuit(backup)  -> Provider backup
                 v all fail
          Static fallback
```

## 2. Configuration

| Setting | Value | Reason |
|---|---:|---|
| failure_threshold | 3 | Opens after repeated provider failures without overreacting to one transient error. |
| reset_timeout_seconds | 2.0 | Allows quick recovery probes during short chaos runs. |
| success_threshold | 1 | One successful half-open probe is enough for this fake provider lab. |
| cache TTL | 300 | Five minutes balances FAQ reuse with freshness. |
| similarity_threshold | 0.92 | High enough to avoid date-sensitive false hits such as 2024 vs 2026. |
| load_test requests | 100 | Enough requests to exercise cache hits and circuit transitions reproducibly. |

## 3. SLO definitions

| SLI | SLO target | Actual value | Met? |
|---|---|---:|---|
| Availability | >= 99% | 1.0 | yes |
| Latency P95 | < 2500 ms | 320.51 | yes |
| Fallback success rate | >= 95% | 1.0 | yes |
| Cache hit rate | >= 10% | 0.7775 | yes |
| Recovery time | < 5000 ms | 2292.3829555511475 | yes |

## 4. Metrics

| Metric | Value |
|---|---:|
| total_requests | 400 |
| availability | 1.0 |
| error_rate | 0.0 |
| latency_p50_ms | 0.35 |
| latency_p95_ms | 320.51 |
| latency_p99_ms | 525.59 |
| fallback_success_rate | 1.0 |
| cache_hit_rate | 0.7775 |
| circuit_open_count | 5 |
| recovery_time_ms | 2292.3829555511475 |
| estimated_cost | 0.041494 |
| estimated_cost_saved | 0.311 |

## 5. Cache comparison

| Metric | Without cache | With cache | Delta |
|---|---:|---:|---|
| latency_p50_ms | 237.6 | 0.3 | -99.9% |
| latency_p95_ms | 510.01 | 320.66 | -37.1% |
| estimated_cost | 0.185782 | 0.041494 | -77.7% |
| cache_hit_rate | 0.0 | 0.7775 | +0.7775 |

## 6. Redis shared cache

In-memory cache is insufficient for multi-instance deployments because each process keeps a separate private cache. `SharedRedisCache` stores entries in Redis with TTL, so separate gateway instances can reuse the same cached response while privacy and false-hit guardrails still apply.

### Evidence of shared state

```
peer.get(...) returned 'shared cache evidence response' with score 1.00
```

### Redis CLI output

```bash
# docker compose exec redis redis-cli KEYS "rl:cache:*"
rl:cache:4918eb19ce89
```

## 7. Chaos scenarios

| Scenario | Expected behavior | Observed behavior | Pass/Fail |
|---|---|---|---|
| primary_timeout_100 | All traffic falls back to backup and the primary circuit opens. | See metrics.json aggregate counters. | pass |
| primary_flaky_50 | Circuit opens under failures and backup serves some traffic. | See metrics.json aggregate counters. | pass |
| all_healthy | Most requests succeed through primary/cache with low errors. | See metrics.json aggregate counters. | pass |
| cache_stale_candidate | Cache hits occur while date/privacy guardrails prevent unsafe hits. | See metrics.json aggregate counters. | pass |

## 8. Failure analysis

Remaining weakness: circuit breaker state is process-local. In production, multiple gateway replicas could disagree about whether a provider is unhealthy, causing extra traffic to an already failing provider.

Change before production: move breaker counters and state transitions to Redis or another shared coordination layer, and add per-provider rate limits around half-open probes.

## 9. Next steps

1. Add concurrent load testing with the configured concurrency value.
2. Store circuit breaker state in Redis for multi-instance consistency.
3. Export Prometheus counters for requests, latency, cache hits, and circuit state.
