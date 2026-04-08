# SmartFeed Architecture

## Overview

SmartFeed builds a paginated feed from multiple async data sources using a tree config.

```
FeedManager.get_feed(session_id, limit, cursor)
  -> executor.run(root_node, ctx, limit, cursor)
    -> tree of nodes execute in parallel
  -> stamp smartfeed_position on results
  -> return FeedResult
```

## Node Types

Three types of nodes:

```
SubFeed (leaf)      -- calls async method from methods_dict
Wrapper (pipeline)  -- fetch -> dedup -> rerank -> cache -> paginate
Mixer (coordinator) -- runs children in parallel, assembles results
```

## Execution Flow

```
                    FeedManager
                        |
                    executor.run()
                        |
              +---------+---------+
              |                   |
         node.execute()    node.build_mix_plan()
         (SubFeed,Wrapper)      (Mixers)
                                  |
                          asyncio.gather(children)
                                  |
                           plan.assemble()
```

### executor.py

Module-level functions (no class):

- `run(node, ctx, limit, cursor)` -- dispatches to `node.execute()` or `node.build_mix_plan()`
- `_execute_mix(plan, ctx, cursor)` -- runs MixPlan children in parallel via `asyncio.gather`

### Wrapper Pipeline

```
fetch child (session_size or limit)
  -> dedup (priority arbitration, seen-set from Redis)
  -> rerank (call methods_dict callable)
  -> cache (write to Redis with TTL)
  -> paginate (slice by page from cursor)
```

Two paths:
- **Cached** (`self.cache` set): warm = read cache + paginate. Cold = full pipeline.
- **Passthrough** (no cache): fetch + dedup (with Redis seen-set) + rerank per page.

### MixPlan

Mixers return `MixPlan(children, assemble)`:

```python
MixChild(node_id="mix_0", node=subfeed_a, demand=8)   # ask for 8 items
MixChild(node_id="mix_1", node=subfeed_b, demand=12)  # ask for 12 items
```

Executor runs all children via `asyncio.gather`, passes results to `assemble(buffers, child_cursors)`.

## Key Models

### Config (Pydantic v2)

```
FeedConfig(version, feed: FeedNode)
FeedNode = Union[SubFeed, Wrapper, MergerPercentage, MergerPositional, ...]
```

Discriminated union on `type` field. Forward refs rebuilt at import time.

### Runtime

```
ExecutionContext(session_id, methods_dict, redis)
FeedResult(data: list, next_page: dict, has_next_page: bool)
```

### Output

```
SmartFeedDebugInfo(source, smartfeed_position, rerank_position?, rrf_score?, ...)
FeedItem(id, _smartfeed_debug_info: SmartFeedDebugInfo)
```

## Redis State

```
sf:{session_id}:{node_id}:{config_hash}        -- cached session data (JSON list)
sf:{session_id}:{node_id}:{config_hash}:meta    -- metadata (gen, child_cursor, child_has_next)
sf:{session_id}:{node_id}:{config_hash}:seen    -- dedup seen-set (Redis SET)
sf:{session_id}:{cache_key}:{hash}              -- shared base cache (for A/B testing)
sf:{session_id}:{node_id}:{hash}:lock           -- distributed lock (SETNX)
```

TTL = inactivity timeout. Refreshed on every access via pipeline EXPIRE.

## Dedup

Two dedup paths, unified `_dedup(data, seen=None)` method:

1. **Cached path** (`_fetch_and_dedup`): overfetch + refill loop. Re-dedup combined batches.
2. **Passthrough path** (`_passthrough`): Redis seen-set for cross-page dedup. SADD new keys after each page.

Priority arbitration: higher `dedup_priority` wins. Equal = first-seen.

## Config Hash

`BaseNode.config_hash()` = md5(model_dump_json(sort_keys=True))[:8].

Used in Redis keys. Any config change = different hash = fresh cache.

## Generation ID

Cached wrapper stamps `gen` (random nonce) in cursor and `:meta`. Stale gen = rebuild from scratch.

## Shared Cache

Two wrappers with same `cache_key` share base data. Each applies its own rerank. RedisLock prevents duplicate fetches.

## File Structure

```
smartfeed/
  models/
    base.py         -- BaseNode, FeedResult, SmartFeedDebugInfo, FeedItem, config_hash
    subfeed.py      -- SubFeed (leaf)
    wrapper.py      -- Wrapper (cache + dedup + rerank pipeline)
    mixers.py       -- Percentage, Positional, Append, Distribute, Gradient
    __init__.py     -- FeedNode union, FeedConfig, exports
  execution/
    executor.py     -- run(), _execute_mix() (module-level functions)
    context.py      -- ExecutionContext
    plans.py        -- MixPlan, MixChild
    redis_lock.py   -- RedisLock (SETNX async context manager)
  manager.py        -- FeedManager (public entry point)
```
