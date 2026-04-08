# SmartFeed

Feed orchestrator. Builds one paginated feed from multiple async data sources using a declarative JSON config.

## Quick Start

```python
from smartfeed.manager import FeedManager

config = {
    "version": "2",
    "feed": {
        "type": "wrapper",
        "node_id": "main",
        "cache": {"session_size": 300, "session_ttl": 300},
        "dedup": {"dedup_key": "id", "overfetch_factor": 3},
        "rerank": {"method_name": "my_rerank"},
        "data": {
            "type": "merger_percentage",
            "node_id": "mix",
            "items": [
                {"percentage": 40, "data": {"type": "subfeed", "subfeed_id": "source_a", "method_name": "source_a"}},
                {"percentage": 60, "data": {"type": "subfeed", "subfeed_id": "source_b", "method_name": "source_b"}}
            ]
        }
    }
}

async def source_a(user_id, limit, next_page, **kwargs):
    # Return FeedResult(data=[...], next_page={...}, has_next_page=True)
    ...

async def my_rerank(items, session_id):
    # Return same items in new order. len(result) == len(items).
    return sorted(items, key=lambda x: x.get("score", 0), reverse=True)

manager = FeedManager(config=config, methods_dict={"source_a": source_a, "source_b": source_b, "my_rerank": my_rerank}, redis_client=redis)
result = await manager.get_feed(session_id="user_123", limit=20, cursor={})
# result.data -- list of items
# result.next_page -- pass back as cursor for next page
# result.has_next_page -- whether more pages available
```

## Node Types

### SubFeed (leaf)

Calls an async function from `methods_dict`.

```json
{"type": "subfeed", "subfeed_id": "tours", "method_name": "get_tours", "dedup_priority": 1}
```

Fields:
- `subfeed_id` -- unique ID, used in cursors and `_smartfeed_debug_info.source`
- `method_name` -- key in `methods_dict`
- `subfeed_params` -- static kwargs passed to method (default: `{}`)
- `raise_error` -- if `false`, swallow errors and return empty (default: `true`)
- `shuffle` -- shuffle results (default: `false`)
- `dedup_priority` -- higher = wins dedup conflicts (default: `0`)

### Wrapper (unified cache + dedup + rerank)

Single node with optional pipeline stages: `fetch -> dedup -> rerank -> cache -> paginate`.

```json
{
  "type": "wrapper",
  "node_id": "pipeline",
  "cache": {"session_size": 300, "session_ttl": 300},
  "dedup": {"dedup_key": "id", "overfetch_factor": 3, "max_refill_loops": 5},
  "rerank": {"method_name": "my_rerank", "raise_error": true},
  "dedup_priority": 3,
  "data": { ... }
}
```

All stages are optional. Combinations:

| cache | dedup | rerank | Behavior |
|-------|-------|--------|----------|
| yes   | no    | no     | Session cache with Redis pagination |
| no    | yes   | no     | Per-page dedup with Redis seen-set |
| yes   | yes   | yes    | Full pipeline: fetch -> dedup -> rerank -> cache |
| yes   | no    | yes    | Rerank + cache (no dedup) |
| no    | no    | yes    | Per-page rerank (no cache, stateless) |

#### Cache

```json
"cache": {"session_size": 300, "session_ttl": 300, "cache_key": null}
```

- `session_size` -- items to fetch from child and cache
- `session_ttl` -- Redis TTL (seconds). Acts as inactivity timeout: refreshed on every access
- `cache_key` -- explicit key for shared cache between wrappers (A/B testing)

#### Dedup

```json
"dedup": {"dedup_key": "id", "missing_key_policy": "error", "overfetch_factor": 3, "max_refill_loops": 5, "state_ttl": 300}
```

- `dedup_key` -- field name for duplicate detection
- `missing_key_policy` -- `"error"` | `"keep"` | `"drop"` (default: `"error"`)
- `overfetch_factor` -- fetch `limit * factor` to compensate for dedup removals (default: `1`)
- `max_refill_loops` -- max refill iterations (default: `20`)
- `state_ttl` -- TTL for Redis seen-set (default: `300`)

Cross-page dedup: seen keys stored in Redis SET `sf:{session_id}:{node_id}:{hash}:seen`.

#### Rerank

```json
"rerank": {"method_name": "my_rerank", "raise_error": true}
```

- `method_name` -- key in `methods_dict`, must be `async def(items, session_id) -> items`
- `raise_error` -- `true` = crash on rerank failure, `false` = keep original order (default: `true`)

Contract: callable returns exactly `len(items)` items. Only reorders.

### Mixer Nodes

**MergerPercentage** -- split by percentage:
```json
{"type": "merger_percentage", "node_id": "mix", "items": [{"percentage": 40, "data": {...}}, {"percentage": 60, "data": {...}}]}
```

**MergerPositional** -- insert at specific positions (1-indexed):
```json
{"type": "merger_positional", "node_id": "pos", "positions": [1, 3, 5, 7], "positional": {...}, "default": {...}}
```

**MergerAppend** -- concatenate children:
```json
{"type": "merger_append", "node_id": "append", "items": [{...}, {...}]}
```

**MergerAppendDistribute** -- round-robin by key:
```json
{"type": "merger_distribute", "node_id": "diverse", "distribution_key": "operator_id", "items": [{...}]}
```

**MergerPercentageGradient** -- shift ratio over pages:
```json
{"type": "merger_percentage_gradient", "node_id": "grad", "item_from": {"percentage": 80, "data": {...}}, "item_to": {"percentage": 20, "data": {...}}, "step": 10, "size_to_step": 30}
```

## dedup_priority

Every node has `dedup_priority: int = 0`. Higher = more important.

- When two items have the same dedup key, the one with higher priority wins
- Equal priority: first-seen wins (order from mixer children list)
- Wrapper/mixer with `dedup_priority != 0` overrides all children in that subtree
- Purely config-based, not stored in items

## _smartfeed_debug_info

SmartFeed stamps metadata on every item:

```python
item["_smartfeed_debug_info"] = {
    "source": "recommended_tours",       # subfeed_id
    "smartfeed_position": 5,             # final position (set by FeedManager)
    "strategy": "model_hot_users",       # from subfeed (optional)
    # Per-wrapper positions:
    "pipeline": {"smartfeed_position": 15, "rerank_position": 3},
    # Rerank fields (set by rerank callable):
    "rerank_position": 1,
    "rrf_score": 0.032,
    "feature_score": 10683198.0,
}
```

## Redis Keys

```
sf:{session_id}:{node_id}:{config_hash}          -- cached data
sf:{session_id}:{node_id}:{config_hash}:meta      -- metadata (child cursor, has_next, gen)
sf:{session_id}:{node_id}:{config_hash}:seen      -- dedup seen-set (SET)
```

`config_hash` = md5 of config subtree JSON. Change any param -> different hash -> fresh cache.

TTL is an inactivity timeout: refreshed on every request. Cache lives while user scrolls.

## Generation ID

Cached wrappers stamp a `gen` nonce in cursor and Redis `:meta`. If user returns with stale `gen` after cache expired -> full rebuild from page 1.

## Subfeed Method Signature

```python
async def my_source(user_id: str, limit: int, next_page: dict, **kwargs) -> FeedResult:
    return FeedResult(data=[...], next_page={"page": 2, "after": ...}, has_next_page=True)
```

## Installation

```
pip install epoch8-smartfeed
```

Requires: Python 3.9+, Pydantic v2, redis (async), orjson.

## Testing

```bash
pytest tests/ -v
```

Requires: fakeredis, pytest-asyncio.
