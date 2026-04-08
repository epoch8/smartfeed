# Changelog

## 0.3.0 (2026-04-08)

SmartFeed v2: complete rewrite.

### Breaking Changes

- Unified `Wrapper` node replaces `MergerViewSession`, `MergerDeduplication`, and external rerank hack
- `FeedManager.get_data()` -> `FeedManager.get_feed(session_id, limit, cursor)`
- `user_id` -> `session_id` in ExecutionContext
- Cursors are plain dicts (not `FeedResultNextPage` Pydantic models)
- `FeedResultClient` removed (use `FeedResult` everywhere)
- `merger_id` renamed to `node_id` in configs
- Config `version: "2"` required
- Pydantic v2 required (v1 compat removed)
- `jsonlib.py` removed (orjson used directly)

### New Features

- **Wrapper** with optional pipeline stages: cache, dedup, rerank
- **Rerank callable** registered in `methods_dict`, referenced by `method_name` in config
- **Cross-page dedup** via Redis seen-set (SET with TTL)
- **Shared cache** via `cache_key` for A/B testing with different rerankers
- **Generation ID** for stale cursor detection
- **TTL touch** (inactivity timeout) on every cache access
- **Overfetch + refill** for dedup in both cached and passthrough paths
- **`raise_error`** on WrapperRerank: fail-hard or fail-soft mode
- **`_smartfeed_debug_info`** stamped automatically: source, positions, per-wrapper data
- **SmartFeedDebugInfo / FeedItem** Pydantic models for typed output
- **`config_hash`** for automatic cache invalidation on config changes

### Removed

- `MergerViewSession` (replaced by `Wrapper(cache=...)`)
- `MergerDeduplication` (replaced by `Wrapper(dedup=...)`)
- `DedupRuntime` (~453 lines)
- `SlotsPlan`, `SlotSpec`, `CallablePlan` (replaced by `MixPlan`)
- `CursorMap` with `merge_delta` / overfetch
- `DeduplicationPolicy`, `CursorSeenStore`, `RedisSeenStore`
- `pydantic_compat.py`
- `jsonlib.py`
- `examples/`
- `mergers/` directory (mixers now in `models/mixers.py`)
- `policies/` directory
- `feed_models.py`, `schemas.py`

### Stats

| Metric | v1 | v2 |
|--------|----|----|
| Python files | 24 | 11 |
| Lines of code | 2838 | ~1300 |
| Test files | 26 | 17 |

## 0.2.0 (2025-11-25)

* Bump dependency versions

## 0.1.0 (2024-12-26)

* Initial build and publish to PyPI
