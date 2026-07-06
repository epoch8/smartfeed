# SmartFeed

Feed orchestrator. Builds one paginated feed from many independent async data sources, described by a declarative JSON/dict config tree. You register your own async fetch functions; SmartFeed handles parallel fetch, cross-source dedup, optional rerank, per-session caching, and cursor-based pagination.

This README is written for engineers evaluating how it works and how much integration effort it takes. For internal architecture and control flow, see `ARCHITECTURE.md`.

- [Install](#install)
- [Quick start](#quick-start)
- [How it works](#how-it-works)
- [Public API](#public-api)
- [Callable contracts you implement](#callable-contracts-you-implement)
- [Config reference](#config-reference)
- [Wrapper pipeline semantics](#wrapper-pipeline-semantics)
- [Dedup and dedup_priority](#dedup-and-dedup_priority)
- [Redis keys and state](#redis-keys-and-state)
- [Error handling and resilience](#error-handling-and-resilience)
- [`_smartfeed_debug_info`](#_smartfeed_debug_info)
- [Integration checklist and footguns](#integration-checklist-and-footguns)
- [Testing](#testing)
- [Integration effort: honest read](#integration-effort-honest-read)

## Install

```
pip install epoch8-smartfeed
```

Requires: Python 3.9+, **pydantic v2**, orjson. An async Redis client (`redis.asyncio.Redis`) is required only if any Wrapper uses `cache` or cross-page `dedup`; without it those features silently fall back to uncached behavior.

## Quick start

```python
from smartfeed.manager import FeedManager
from smartfeed.models.base import FeedResult

config = {
    "version": "2",
    "feed": {
        "type": "wrapper",
        "node_id": "main",
        "cache": {"session_size": 300, "session_ttl": 300},
        "dedup": {"dedup_key": "id"},
        "rerank": {"method_name": "my_rerank"},
        # NOTE: cache_key is a field on the wrapper, NOT inside "cache".
        # "cache_key": "shared_pool",
        "data": {
            "type": "merger_percentage",
            "node_id": "mix",
            "items": [
                {"percentage": 40, "data": {"type": "subfeed", "subfeed_id": "source_a", "method_name": "source_a"}},
                {"percentage": 60, "data": {"type": "subfeed", "subfeed_id": "source_b", "method_name": "source_b"}},
            ],
        },
    },
}

# A subfeed fetch function. **kwargs is REQUIRED (see Callable contracts).
async def source_a(user_id, limit, next_page, **kwargs):
    # ... fetch your data ...
    return FeedResult(data=[{"id": 1}, {"id": 2}], next_page={"page": 2}, has_next_page=True)

async def source_b(user_id, limit, next_page, **kwargs):
    return FeedResult(data=[{"id": 3}], next_page={}, has_next_page=False)

# A rerank function. MUST return exactly len(items) items (reorder only).
async def my_rerank(items, session_id):
    return sorted(items, key=lambda x: x.get("score", 0), reverse=True)

manager = FeedManager(
    config=config,
    methods_dict={"source_a": source_a, "source_b": source_b, "my_rerank": my_rerank},
    redis_client=redis,  # async redis.asyncio.Redis; needed for cache/dedup persistence
)

result = await manager.get_feed(session_id="user_123", limit=20, cursor=None)
# result.data          -> list of item dicts, each stamped with _smartfeed_debug_info
# result.next_page      -> opaque cursor; pass back verbatim as `cursor` next call
# result.has_next_page  -> bool
```

Config is validated when `FeedManager` is constructed. A malformed config raises `pydantic.ValidationError` at construction, not on the first `get_feed`.

## How it works

A feed is a tree of nodes described in config. There are three node kinds (seven concrete `type` values):

- **SubFeed** (`subfeed`) — a leaf. Calls one of your async functions from `methods_dict` and adapts its result into SmartFeed's shape, tagging each item with its source.
- **Wrapper** (`wrapper`) — a pipeline over one child. Optional stages applied in order: dedup, rerank, cache, paginate. Each stage is independent.
- **Mixer** — a combiner over several children, run concurrently and merged by a rule. Five concrete types: `merger_percentage`, `merger_append`, `merger_positional`, `merger_percentage_gradient`, `merger_distribute`. "Mixer" is a documentation umbrella, not a class.

The executor walks the tree. For each node it either calls `execute()` (SubFeed, Wrapper) or builds a mix plan and runs the children concurrently via `asyncio.gather`, then assembles them. Nodes nest arbitrarily. Each node paginates independently: the returned `next_page` cursor is a dict keyed per node, and you round-trip it verbatim.

"Concurrent" here means `asyncio` concurrency on one event loop (sibling sources are awaited together, so page latency is close to the slowest source, not the sum). It is not multi-core.

## Public API

The entire public surface is one class with two methods.

```python
FeedManager(config: dict, methods_dict: dict, redis_client: Optional[Any] = None)
```
Parses and validates `config` into a `FeedConfig` immediately (pydantic v2). `methods_dict` maps names to your async fetch/rerank callables. `redis_client` must be an async `redis.asyncio.Redis`; if `None`, every Wrapper with `cache`/`dedup` degrades to uncached passthrough. Holds no per-request state.

```python
async get_feed(session_id: str, limit: int, cursor: Optional[dict] = None) -> FeedResult
```
`cursor=None` is treated as the first page (`{}`). Builds a fresh execution context per call and delegates to the executor, then stamps each dict item's final 0-based page position. Returns `FeedResult(data, next_page, has_next_page)`. `next_page` is opaque; store it and pass it back as `cursor`.

There is **no request-parameters channel**: only `session_id`, `limit`, and the cursor are threaded to sources. Per-request context must be baked into `subfeed_params` when you build the config, or captured in closures when you build `methods_dict` for that request.

`FeedResult` (`smartfeed.models.base`): `data: list`, `next_page: dict`, `has_next_page: bool`. `data` is a bare list; the pipeline assumes dict items and silently skips non-dict items in stamping/dedup.

## Callable contracts you implement

### SubFeed fetch function

```python
async def fetch(user_id: str, limit: int, next_page: dict, **kwargs) -> FeedResult: ...
```

- **`**kwargs` is mandatory.** The executor injects `ctx=` into every call. A function without `**kwargs` raises `TypeError` on every invocation. (And if that source has `raise_error=False`, the `TypeError` is swallowed into an empty result, so a signature bug looks exactly like "this source had no items today".)
- `user_id` is the feed `session_id` (not necessarily an account id).
- `limit` is this leaf's computed demand for the page. Under a mixer it is a fraction of the page; under a dedup wrapper it is inflated by `overfetch_factor` or a refill deficit.
- `next_page` is this subfeed's own cursor slice (`cursor.get(subfeed_id, {})`), opaque. Echo back whatever you need on the next page.
- Return a `FeedResult` (or any object exposing `.data` / `.next_page` / `.has_next_page`; there is no `isinstance` check).
- On success each dict item is stamped with `_smartfeed_debug_info.source = subfeed_id`. Non-dict items are passed through untouched.

### Rerank function

```python
async def rerank(items: list, session_id: str) -> list: ...
```

- Must return a list of **exactly `len(items)`** items. Reordering or replacing is allowed; adding or dropping is not — a length mismatch always raises `ValueError`, even with `raise_error=False`.
- `items` are already fetched and deduped, in pre-rerank order.
- Exceptions thrown by your function are governed by `rerank.raise_error` (see [Error handling](#error-handling-and-resilience)).

## Config reference

All defaults below are byte-exact from the code. `FeedConfig` is the top-level shape:

| Field | Type | Default | Meaning |
|---|---|---|---|
| `version` | `str` | required | Descriptive tag. Carried but never read by the code. |
| `feed` | `FeedNode` | required | Root node. Discriminated union on `type` over the 7 node types. |

### SubFeed — `type: "subfeed"`

| Field | Type | Default | Meaning |
|---|---|---|---|
| `subfeed_id` | `str` | required | Logical name. Stamped as `_smartfeed_debug_info.source`, is the cursor namespace key, and the identity dedup priority resolves against. |
| `method_name` | `str` | required | Key into `methods_dict`. Looked up **before** the try block, so a missing/typo'd name raises `KeyError` even with `raise_error=False`. |
| `subfeed_params` | `dict` | `{}` | Static kwargs merged into the fetch call. A key colliding with `user_id`/`limit`/`next_page`/`ctx` raises `TypeError` at call time (not validated at parse time). |
| `raise_error` | `bool` | `True` | `True`: exceptions propagate. `False`: swallow, return an empty page. On the swallow path `next_page` is the full incoming cursor unchanged (not re-nested under `subfeed_id`). |
| `shuffle` | `bool` | `False` | Shuffle this source's page in place before stamping. Per-page only. |
| `dedup_priority` | `int` | `0` | See [dedup_priority](#dedup-and-dedup_priority). |

### Wrapper — `type: "wrapper"`

Wraps exactly one child with optional stages. `cache`, `dedup`, `rerank` are all independent and optional (all 8 on/off combinations parse).

| Field | Type | Default | Meaning |
|---|---|---|---|
| `node_id` | `str` | required | Namespaces this wrapper's cursor, its cache keys (when `cache_key` unset), and its debug sub-dict. |
| `data` | node | required | The single child node. |
| `cache` | `WrapperCache?` | `None` | Enables per-session caching. Requires a live `redis_client` too, else passthrough. |
| `dedup` | `WrapperDedup?` | `None` | Enables duplicate removal with overfetch/refill. |
| `rerank` | `WrapperRerank?` | `None` | Enables a rerank pass (after fetch+dedup). |
| `cache_key` | `str?` | `None` | **Sibling of `cache`, not inside it.** Enables a shared base cache across wrappers with the same key (see [Wrapper pipeline](#wrapper-pipeline-semantics)). No-op unless `cache` is also set. |
| `dedup_priority` | `int` | `0` | See below. |

`WrapperCache`:

| Field | Type | Default | Meaning |
|---|---|---|---|
| `session_size` | `int` | `300` | Items built and cached per session (cold build). Pages are sliced from this. |
| `session_ttl` | `int` | `300` | Redis TTL (s) on the data + `:meta` keys. Refreshed on every warm read — an inactivity timeout, not a fixed expiry. |

`WrapperDedup`:

| Field | Type | Default | Meaning |
|---|---|---|---|
| `dedup_key` | `str` | required | Item field for identity. Compared as `str(item[key])`, so `1` and `"1"` collide. |
| `missing_key_policy` | `"error" \| "keep" \| "drop"` | `"error"` | When an item lacks `dedup_key`: `error` raises, `keep` passes it through un-deduped, `drop` discards it. |
| `overfetch_factor` | `int` | `4` | **Deprecated, no effect.** The dedup fetch now pulls exactly the outstanding deficit and refills until the target is filled or the child is exhausted. Kept for config back-compat. |
| `max_refill_loops` | `int` | `2` | **Deprecated, no effect.** Refill now continues until the page is full or the source runs out (bounded by an internal safety cap), so pages are not cut short. |
| `state_ttl` | `int` | `300` | TTL (s) on the Redis seen-set (session-scoped dedup state, both cache and passthrough paths). |

`WrapperRerank`:

| Field | Type | Default | Meaning |
|---|---|---|---|
| `method_name` | `str` | required | Key into `methods_dict`. `KeyError` if missing. |
| `raise_error` | `bool` | `True` | Governs only exceptions thrown by your rerank function. Does **not** govern the length-mismatch check, which always raises. |

### Mixers

**MergerPercentage** — `type: "merger_percentage"`. Splits the page by fixed percentages, then concatenates a solid block per branch in config order (it does not interleave).

```json
{"type": "merger_percentage", "node_id": "mix",
 "items": [{"percentage": 40, "data": {…}}, {"percentage": 60, "data": {…}}]}
```
| Field | Type | Default | Meaning |
|---|---|---|---|
| `node_id` | `str` | required | Prefix for internal child ids. |
| `items` | `list[{percentage:int, data:node}]` | required | Per-branch demand = `limit * percentage // 100`. Rounding remainder is topped up (largest-remainder) **only when the percentages sum to exactly 100**. `percentage` has no range validator; sum is not enforced. |
| `dedup_priority` | `int` | `0` | |

There is no `[:limit]` trim: percentages summing over 100 overflow the page, under 100 underfill.

**MergerAppend** — `type: "merger_append"`. Equal-share split, concatenated in order, then trimmed to `limit`.

```json
{"type": "merger_append", "node_id": "append", "items": [{…}, {…}]}
```
| Field | Type | Default | Meaning |
|---|---|---|---|
| `node_id` | `str` | required | |
| `items` | `list[node]` | required | Each child gets `limit // n`; the remainder goes one-each to the first children. Empty `items` yields an empty page (no error). |
| `dedup_priority` | `int` | `0` | |

**MergerPositional** — `type: "merger_positional"`. Places `positional` items at fixed slots, `default` items everywhere else.

```json
{"type": "merger_positional", "node_id": "pos",
 "positions": [1, 3, 5, 7], "positional": {…}, "default": {…}}
```
| Field | Type | Default | Meaning |
|---|---|---|---|
| `node_id` | `str` | required | |
| `positions` | `list[int]` | `[]` | 1-indexed slots, relative to **each page's** `1..limit` window (they repeat every page; they are not absolute feed positions). |
| `positional` | node | required | Fills the pinned slots. Demand = count of in-range positions. |
| `default` | node | required | Fills the rest. Demand = `limit - pos_count`, fixed regardless of actual returns. |
| `dedup_priority` | `int` | `0` | |

If a source runs dry the other backfills; if the positional source is exhausted the page can underfill (default demand is fixed).

**MergerPercentageGradient** — `type: "merger_percentage_gradient"`. A two-child percentage merger whose ratio shifts by `step` points every `size_to_step` cumulative items, tracked across pages via its own cursor counter.

```json
{"type": "merger_percentage_gradient", "node_id": "grad",
 "item_from": {"percentage": 80, "data": {…}}, "item_to": {"percentage": 20, "data": {…}},
 "step": 10, "size_to_step": 30}
```
| Field | Type | Default | Meaning |
|---|---|---|---|
| `node_id` | `str` | required | Also the merged-cursor key holding `{"page": n}`. |
| `item_from` | `{percentage:int, data:node}` | required | Starting branch. Its share falls toward 0. |
| `item_to` | `{percentage:int, data:node}` | required | Ending branch. Its share rises toward 100, then clamps. |
| `step` | `int` | required | Percentage points shifted per segment. |
| `size_to_step` | `int` | required | Items per shift. Used as a `range()` step; `0` raises at runtime, not at parse time. |
| `dedup_priority` | `int` | `0` | |

**MergerAppendDistribute** — `type: "merger_distribute"` (note the literal). Round-robin by a key so no two adjacent items share it.

```json
{"type": "merger_distribute", "node_id": "diverse",
 "distribution_key": "operator_id", "sorting_key": "score", "sorting_desc": true, "items": [{…}]}
```
| Field | Type | Default | Meaning |
|---|---|---|---|
| `node_id` | `str` | required | |
| `items` | `list[node]` | required | **Every child is asked for the full `limit`** (up to `N * limit` fetched), then pooled and trimmed to `limit`. Higher backend load than items shown. |
| `distribution_key` | `str` | required | Group key. `entry[key]` with no missing-key policy — `KeyError` if an item lacks it. |
| `sorting_key` | `str?` | `None` | If set, the whole pool is sorted by this key before grouping. `KeyError` if missing. |
| `sorting_desc` | `bool` | `False` | Sort direction. Ignored if `sorting_key` is `None`. |
| `dedup_priority` | `int` | `0` | |

## Wrapper pipeline semantics

A Wrapper has two paths, chosen at runtime:

- **Cached** (`cache` set **and** `redis_client` present). First request for a session is a cold build: fetch `session_size` items, dedup, rerank, store the batch in Redis, serve page 1. Later pages slice out of the stored batch with no refetch. When the batch is exhausted the pipeline continues from where the child left off (it does not restart the source). Concurrent first requests for a session are serialized by a cold-build lock, so the child is fetched once. Rerank runs once per cold build over the whole batch, not per page.
- **Passthrough** (no `cache`, or no Redis). The pipeline reruns every page.

**Dedup contract (session-scoped).** Within one continuous forward scroll, a user sees each item at most once — no matter how far they scroll, and across the internal rebuild boundaries the cursor crosses. Dedup is scoped to the session, not across sessions: re-sending an empty cursor (or page 1) starts a **fresh** scroll and may repeat items already shown in a previous scroll. Both paths honor this via a Redis seen-set (`:seen`): the passthrough path persists shown ids across pages; the cached path carries the seen-set across cold rebuilds and resets it when a fresh scroll begins (empty cursor). Neither path over-fetches, so no unique item is silently skipped, and refill fills each page to `limit` unless the source is genuinely exhausted.

**Shared base cache (`cache_key`).** Two wrappers with different `node_id` but the same `cache_key` share one fetched+deduped base dataset (keyed by `cache_key` + the child's config hash + the continuation position), each applying its own rerank on top. Each page-window is its own shared segment, so a shared wrapper paginates past `session_size` normally. A distributed lock ensures only one caller does each cold fetch. Caveat: whichever wrapper wins the cold-build race bakes in **its** `session_size` and dedup settings for all sharers; a sibling with different settings reads that same pool. The shared base dedups within each segment; for strict cross-scroll uniqueness prefer a non-shared cached wrapper. Use `cache_key` for A/B testing two rerankers over one item pool; do not rely on per-sharer dedup/size differences.

## Dedup and dedup_priority

Every node has `dedup_priority: int = 0`. When a Wrapper's dedup sees the same key from two sources, the item whose source subtree has the higher effective priority wins; equal priority means first-seen wins (order follows the mixer's child list). Priority propagates down: a node with a non-zero `dedup_priority` overrides its whole subtree unless a descendant sets its own non-zero value. It is config-only and never written onto items.

## Redis keys and state

All SmartFeed state lives in Redis under `sf:{session_id}:...`. `config_hash` is the first 8 hex chars of an md5 over the node's sorted JSON dump, so any config change moves the key and old data ages out.

```
sf:{session_id}:{cache_key or node_id}:{config_hash}          cached session batch (JSON)
sf:{session_id}:{cache_key or node_id}:{config_hash}:meta     gen nonce, child cursor, child has_next
sf:{session_id}:{cache_key or node_id}:{config_hash}:coldlock cold-build lock (ttl 30s)
sf:{session_id}:{node_id}:{config_hash}:seen                  dedup seen-set (session-scoped; cache + passthrough)
sf:{session_id}:{cache_key}:{child_config_hash}:{segment}     shared base segment (when cache_key set)
sf:{session_id}:{cache_key}:{child_config_hash}:{segment}:meta   shared base segment meta
sf:{session_id}:{cache_key}:{child_config_hash}:{segment}:lock   shared segment cold-build lock (ttl 30s)
```

(`{segment}` is `"0"` for page 1 and a short hash of the continuation cursor for later windows.)

TTL (`session_ttl`) is refreshed on every warm read, so an active user's cache does not expire mid-scroll. The `:seen` set has its own `state_ttl`. Each cold build stamps a random `gen` nonce into the cursor and `:meta`; if a returning cursor's `gen` no longer matches (cache expired or rebuilt), SmartFeed rebuilds cleanly from page 1 rather than resuming into an inconsistent state.

Note the `next_page` cursor shape is config-dependent: a cached wrapper hides the child cursor behind `{node_id: {offset, gen}}` (an absolute offset, so a client may change `limit` between pages without skipping or repeating items), an uncached wrapper exposes the raw child cursor, and the gradient injects `{node_id: {page}}`. Adding or removing a `cache` block changes the cursor your clients round-trip.

## Error handling and resilience

Error handling is **opt-in per node**. By default (`raise_error=True` everywhere) one failing source or rerank fails the entire `get_feed` call, including sibling branches that already succeeded — `asyncio.gather` runs without `return_exceptions=True`, and `FeedManager` has no try/except of its own. For fail-soft behavior, set `raise_error=False` on the specific SubFeed/rerank nodes.

Four sharp edges an integrator will hit:

1. **Missing `method_name`** is looked up before the try block, so it raises `KeyError` regardless of `raise_error`.
2. **Rerank length mismatch** always raises `ValueError`, regardless of `rerank.raise_error`.
3. **A subfeed function without `**kwargs`** raises `TypeError` on every call (the injected `ctx` has nowhere to go). With `raise_error=False` this is swallowed into an empty page — a signature bug that reads as "no items".
4. **`subfeed_params` key collisions** (with `user_id`/`limit`/`next_page`/`ctx`) raise `TypeError` at call time, with the same silent-swallow caveat under `raise_error=False`.

Silent behavior downgrades (no exception): `cache` without a `redis_client`; `cache_key` without `cache`; divergent shared-`cache_key` settings. These ship without error and only show up as degraded cache/dedup metrics.

## `_smartfeed_debug_info`

Every dict item carries a `_smartfeed_debug_info` bundle. The core writes these (all positions are **0-based** at runtime — the model comments saying "1-based" are stale):

- `source` — the producing `subfeed_id`.
- `smartfeed_position` — final 0-based page position (set by `FeedManager`). Wrappers also stamp a pre-rerank position under their `node_id`.
- `rerank_position` — 0-based post-rerank index under the wrapper's `node_id`, only when rerank is configured.

Other documented fields (`strategy`, `rrf_score`, `feature_score`, `feature_position`, `total_reranked`, `raw_params`) are **conventions only**; the core never writes them. Your rerank callable may attach them (the bundle allows extra keys). `FeedItem` and `SmartFeedDebugInfo` are typing aids and are never instantiated at runtime — the pipeline operates on plain dicts.

## Integration checklist and footguns

What you must provide:

- One async fetch function per `method_name`, each accepting `**kwargs` and returning a `FeedResult`.
- Any rerank functions, each returning exactly `len(items)` items.
- The `methods_dict` (a plain dict; no registration API).
- The config tree. pydantic checks types and shapes only; **semantic correctness is yours** — matching `dedup_key`s across siblings, percentages summing to 100, `cache_key` placed on the Wrapper, distribution/sorting keys present on items.
- An async `redis.asyncio.Redis` if you want caching or cross-page dedup persistence.
- Explicit `raise_error=False` on each node you want to fail soft (no global switch).
- Keep cached items orjson-serializable (`str/int/float/bool/None/list/dict`). A `Decimal` that works in passthrough throws once caching serializes the batch.

Footguns, condensed:

- `**kwargs` is load-bearing on every subfeed function (absorbs injected `ctx`).
- `cache_key` goes on the Wrapper, not inside `cache`, and is a no-op without `cache`.
- `cache` without Redis, and shared `cache_key` with divergent settings, degrade silently.
- Percentage mixers do not interleave and do not enforce sum==100; positional slots are per-page; distribute over-fetches and has no missing-key policy.
- Cursor shape depends on whether a `cache` block is present.

## Testing

- `fakeredis.aioredis.FakeRedis()` is the standard fixture; the whole integration is testable without a real Redis.
- `pytest` + `pytest-asyncio` (`@pytest.mark.asyncio`). Both are pinned dev deps.
- `tests/conftest.py` has a minimal `METHODS` dict (items / empty / error) as a starting point.
- There is no built-in validator for your subfeed signatures. Add a smoke test that runs each configured method through `get_feed` once before deploy — specifically to catch the `**kwargs`/`ctx` and `subfeed_params`-collision classes, which surface only at call time.

```
pytest tests/ -v
```

