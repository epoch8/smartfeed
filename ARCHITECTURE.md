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

- `run(node, ctx, limit, cursor)` -- dispatches on `isinstance(node, MixerNode)`: mixers build a MixPlan, leaf/pipeline nodes execute()
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

Items are plain dicts; each carries a `_smartfeed_debug_info` dict bundle
(source, smartfeed_position, rerank_position when reranked -- all 0-based).

## Redis State

```
sf:{session_id}:{cache_key or node_id}:{config_hash}           -- cached session batch (Redis LIST of orjson items)
sf:{session_id}:{cache_key or node_id}:{config_hash}:meta      -- metadata (gen, child_cursor, child_has_next)
sf:{session_id}:{cache_key or node_id}:{config_hash}:coldlock  -- cold-build lock (SETNX, ttl 10s)
sf:{session_id}:{node_id}:{config_hash}:seen                   -- session-scoped dedup seen-set (Redis SET)
sf:{session_id}:{cache_key}:{child_hash}:{segment}       -- shared base segment (blob, per continuation window)
sf:{session_id}:{cache_key}:{child_hash}:{segment}:meta  -- shared segment meta (child cursor / has_next)
sf:{session_id}:{cache_key}:{child_hash}:{segment}:lock  -- shared segment cold-build lock (SETNX, ttl 10s)
```

Warm page reads are one MULTI/EXEC pipeline: GET meta + LLEN + LRANGE of the page
window + EXPIRE on data/meta/seen -- an atomic snapshot that transfers only the
requested window, never the whole batch.

TTL = inactivity timeout. Refreshed on every access via pipeline EXPIRE.
Cursor for a cached wrapper is `{node_id: {offset, gen}}` (absolute offset).

## Dedup

Two dedup paths, unified `_dedup(data, seen=None)` method. Both fetch exactly the
outstanding deficit and refill until the target is filled or the child is exhausted
(no over-fetch, so no item loss; no early give-up, so no short pages), and both carry
a session-scoped Redis seen-set so dedup holds across the whole scroll:

1. **Cached path** (`_fetch_and_dedup` / `_cold_build`): seen-set carried across cold
   rebuilds, reset on a fresh scroll (empty cursor).
2. **Passthrough path** (`_passthrough`): seen-set persisted across pages; SADD after each page.

Priority arbitration: higher `dedup_priority` wins even when the higher-priority copy is
not first-seen; equal priority = first-seen.

## Config Hash

`BaseNode.config_hash()` = md5(json.dumps(model_dump(), sort_keys=True, default=str))[:8],
memoized per node instance (config is immutable after validation).

Used in Redis keys. Any config change = different hash = fresh cache.

## Generation ID

Cached wrapper stamps `gen` (random nonce) in cursor and `:meta`. Stale gen = rebuild from scratch.

## Shared Cache

Two wrappers with same `cache_key` share base data. Each applies its own rerank. RedisLock prevents duplicate fetches.

## File Structure

```
smartfeed/
  models/
    base.py         -- BaseNode, MixerNode, FeedResult, config_hash
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
