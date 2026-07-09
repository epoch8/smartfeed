# Changelog

## 1.0.0 (2026-07-07)

SmartFeed v2: complete rewrite. Changes below are relative to the last published release, **0.2.0**. (The pre-rewrite internal working tree contained additional intermediate modules — never part of a published release — that are not listed here.) See [MIGRATION.md](MIGRATION.md) for step-by-step upgrade instructions.

### New Features

- **Wrapper** with optional pipeline stages: cache, dedup, rerank
- **Session-scoped dedup contract**: within one continuous scroll a user sees each item at most once, no matter how far they scroll; an empty cursor starts a fresh scroll
- **Cross-page dedup** via Redis seen-set (SET with TTL)
- **Deficit-based refill**: dedup returns full pages without discarding unique items (both cached and passthrough paths)
- **Rerank callable** registered in `methods_dict`, referenced by `method_name` in config
- **Shared cache** via `cache_key` for A/B testing with different rerankers
- **Generation ID** for stale cursor detection
- **TTL touch** (inactivity timeout) on every cache access
- **`raise_error`** on WrapperRerank: fail-hard or fail-soft mode
- **`_smartfeed_debug_info`** stamped automatically: source, positions, per-wrapper data (plain dicts, all positions 0-based)
- **`config_hash`** for automatic cache invalidation on config changes
- **Config validation at construction**: duplicate `subfeed_id`/`node_id` anywhere in the tree are rejected (they share one cursor/Redis namespace); gradient `step`/`size_to_step` must be > 0
- **Input validation**: `get_feed` rejects non-positive `limit`, empty `session_id`, non-dict `cursor`; malformed per-node cursor contents (tampered `offset`/`gen`/`page`) raise `ValueError` naming the node
- **Session batch stored as a Redis LIST**: warm page reads transfer and parse only the requested window (single atomic pipeline: meta + LLEN + LRANGE + TTL refresh)
- **PEP 561 `py.typed` marker**: annotations are visible to downstream type checkers

### Breaking Changes

- Models moved from `smartfeed.schemas` to `smartfeed.models` (`FeedManager` keeps its name)
- `FeedManager.get_data(user_id, limit, next_page, **params)` -> `FeedManager.get_feed(session_id, limit, cursor)`
- `user_id` -> `session_id` in ExecutionContext
- Cursors are plain dicts, not `FeedResultNextPage` Pydantic models
- Config: `merger_view_session` -> unified `wrapper` node with optional `cache` / `dedup` / `rerank` stages (`session_live_time` -> `cache.session_ttl`)
- Config: `merger_id` -> `node_id` on merger and wrapper nodes (subfeed `subfeed_id` is unchanged)
- Config: merger-level `shuffle` removed; only `SubFeed.shuffle` remains
- Config: `MergerPositional.start` / `end` / `step` removed; `positions` are now page-relative (were absolute across pages)
- Config: top-level `version` is no longer required or read (unknown top-level keys are ignored, so configs carrying it still parse)
- `FeedResultClient`, `FeedResultNextPage` removed (use `FeedResult` and plain-dict cursors)
- Pydantic v2 required (v1 support removed)

### Removed (from the 0.2.0 public surface)

- `smartfeed.schemas` module (models now live in `smartfeed.models`)
- `MergerViewSession` node (replaced by `Wrapper(cache=..., dedup=...)`)
- merger-level `shuffle`; `MergerPositional.start` / `end` / `step`
- `FeedResultClient`, `FeedResultNextPage`
- `examples/`

### Stats (v2 vs the pre-rewrite internal working tree)

| Metric | pre-rewrite | v2 |
|--------|-------------|----|
| Python files | 24 | 11 |
| Lines of code | 2838 | ~1650 |
| Test files | 26 | 43 |

## 0.2.0 (2025-11-25)

* Bump dependency versions

## 0.1.0 (2024-12-26)

* Initial build and publish to PyPI
