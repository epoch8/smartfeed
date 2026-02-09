# SmartFeed Architecture (medium-brief)

## 1) What SmartFeed does

SmartFeed builds one paginated feed from multiple client-provided sources (“subfeeds”) using a declarative tree config:

- **Leaf**: `SubFeed` (calls one client method)
- **Mergers**: compose children (`append`, `distribute`, `positional`, `percentage`, `percentage_gradient`, `view_session`)
- **Wrapper**: `MergerDeduplication` (changes execution semantics around one child)

Core runtime:

- parse config -> create request `ExecutionContext` -> run tree via shared `Executor` -> return `FeedResult` + `next_page`.


## 2) Public surfaces and core data types

### Public entrypoint

- `FeedManager(config, methods_dict, redis_client=None)`
  - `get_data(user_id, limit, next_page, **params) -> FeedResult`

`methods_dict` maps config `method_name` strings to host-app callables.

### Config schema surface

`smartfeed.schemas` keeps stable imports for:

- `FeedConfig`: top-level model (`version`, `feed`)
- `FeedTypes`: discriminated union by `type`

### Cursor / pagination models

- `FeedResultNextPageInside`: one node cursor (`page`, `after`)
- `FeedResultNextPage`: full-tree cursor map (`data: {node_id -> FeedResultNextPageInside}`)

### Result models

- `FeedResultClient`: required return type of client subfeed methods
- `FeedResult`: normalized return type of any SmartFeed node


## 3) Node interface contract

All nodes inherit `BaseFeedConfigModel` and are executed through:

- `get_data(methods_dict, user_id, limit, next_page, redis_client=None, ctx=None, **params) -> FeedResult`

Important notes:

- If a node implements `build_plan(...)`, executor uses the plan path.
- Base `get_data(...)` delegates back to executor and expects `build_plan(...)` to exist.
- Every node has `dedup_priority: int` (used by dedup arbitration/refill ordering).


## 4) ExecutionContext

`ExecutionContext` is per-request state propagated through the tree:

- `methods_dict`, `user_id`, `redis_client`
- `executor` (lazy via `ensure_executor()`)
- optional policy/settings:
  - `dedup`: `DeduplicationPolicy` when dedup wrapper is active
  - `refill_settings`: `RefillExecutionSettings(overfetch_factor, max_refill_loops)`

Responsibilities:

- centralize shared plumbing (executor + redis client)
- keep execution policies out of user params


## 5) Executor (runtime engine)

Primary entry:

- `Executor.run(node, ctx, limit, next_page, **params) -> FeedResult`

Execution strategy:

1. **Plan-first**
   - `build_plan(...)` -> execute returned `Plan`
   - otherwise call node `get_data(...)`
2. **Centralized concurrency**
   - child runs use executor-managed `asyncio.gather(...)`
3. **Dedup/refill hooks**
   - for non-slot nodes with `ctx.dedup`, run `DedupRuntime.run_node_with_dedup_refill(...)`
   - for `SlotsPlan`, dedup/refill is handled inside slot execution

`SlotsPlan` execution highlights:

1. collect unique owners + demand per owner
2. fetch owners concurrently (with optional `owner_fetch_limits` overrides)
3. merge only changed cursor keys (`CursorMap.merge_delta`)
4. apply:
   - dedup arbitration + refill (`apply_slots_plan_dedup`) when `ctx.dedup` exists
   - refill-only deficits (`apply_slots_plan_refill`) when only `ctx.refill_settings` exists
5. consume slot schedule and call `assemble(...)`

When dedup is active for a slots plan, owners are executed with `dedup=None` in owner context so global arbitration stays centralized.


## 6) Plans: declarative execution

Plans separate “what to run” from “how to run it”.

- `CallablePlan(fn)`
  - node-provided async function with custom flow, still executed by executor

- `SlotsPlan(ctx, limit, next_page, params, slots, assemble, owner_fetch_limits=None)`
  - `slots`: ordered `SlotSpec(owner, max_count)` schedule
  - `assemble(output, merged_next_page, owner_results)`: builds final `FeedResult`


## 7) Mergers and leaf responsibilities

### SubFeed (leaf)

- derives its local cursor from `next_page.data[subfeed_id]` (defaults page=1/after=None)
- calls `methods_dict[method_name]`
- passes only params present in method signature + `subfeed_params`
- async methods are awaited; sync methods run via `asyncio.to_thread(...)`
- `raise_error=False` converts method failure into empty `FeedResultClient`
- optional `shuffle` then normalizes to `FeedResult`

### Slot-based mergers

These build `SlotsPlan`:

- `MergerAppend`: concatenation (optional shuffle)
- `MergerAppendDistribute` (`type="merger_distribute"`): append then redistribute by `distribution_key`
- `MergerPositional`: page-local slot ownership for `positional` vs `default`, keeps its own merger cursor
- `MergerPercentage`: integer allocation by percentages; when total is exactly 100, remainder is distributed to avoid underfill
- `MergerPercentageGradient`: two-owner percentage curve across the page, then advances merger page cursor

### MergerViewSession (Redis-backed session cache)

Goal: cache a session-sized list and serve slices.

Flow:

1. build cache key: `{merger_id}_{user_id}` + optional suffix from `custom_view_session_key`
2. check Redis `exists`; if no cache or no merger cursor in request -> regenerate session
3. on hit, `get`; if Redis returns `None` unexpectedly, regenerate
4. on generation: execute child once for `session_size`, optional dedup, store JSON with TTL
5. return page slice and increment merger cursor page
6. optional `shuffle` is applied to returned page slice (cache payload is not reshuffled)

### MergerDeduplication (single-child wrapper)

Goal: deduplicate while keeping child mix/slot semantics.

Key behavior:

- fresh session when merger cursor is absent or `page <= 0`
  - reset descendant cursors
  - for Redis backend, reset Redis seen-state key
- seen-state backend:
  - `cursor`: encoded into merger cursor `after`
  - `redis`: ZSET `dedup:{merger_id}:{user_id}` (+ optional custom suffix)
- builds `DeduplicationPolicy` + child `ExecutionContext(dedup=..., refill_settings=...)`
- executes child via shared executor, commits store, writes merger cursor (`page+1`, `after` for cursor backend)

Refill/overfetch behavior:

- duplicates trigger bounded refill loops (`max_refill_loops`)
- overfetch (`overfetch_factor`) is applied only for rewindable integer-offset cursors
- when overfetch is used, leaf cursor is rewound to inspected-count to avoid skipping unseen items


## 8) Dedup policy + seen stores

### DeduplicationPolicy

Owns key extraction + acceptance rules:

- entity key from `dedup_key` + `missing_key_policy`
- reject duplicates already seen in current response (`seen_request_set`)
- compare candidate priority vs persisted seen priority

Capabilities:

- batched prefetch from store
- per-owner arbitration with deterministic tie-break: `(-dedup_priority, owner_rank, item_rank)`
- ordered single-stream acceptance (`accept_batch`) returning accepted items + inspected count

### Seen stores

- `CursorSeenStore`
  - in-cursor map of `{key -> max_priority}`
  - optional compression + max-key trimming at commit

- `RedisSeenStore`
  - cached reads via `redis_zmscore(...)`
  - buffered writes via `redis_zadd_and_expire(...)`


## 9) Redis/JSON helpers

- `_redis_call(client, method_name, *args, **kwargs)`
  - async redis client: direct await
  - sync redis client: `asyncio.to_thread(...)`

Other helpers:

- `jsonlib`: thin `orjson` wrapper compatible with package usage (`dumps`/`loads`)
- `dedup_utils`: cursor encode/decode + Redis ZSET helper fallbacks (`zmscore` / pipeline)


## 10) End-to-end call flows

### A) Standard request (no view session, no dedup)

1. `FeedManager.get_data(...)` builds `ExecutionContext`
2. `Executor.run(root, ctx, limit, next_page)`
3. recursive execution via plans or direct `get_data(...)`
4. returns `FeedResult(data, next_page, has_next_page)`

### B) Slot-based merger request

1. merger returns `SlotsPlan`
2. executor fetches owners concurrently
3. optional arbitration/refill runs
4. slots are consumed in schedule order
5. `assemble(...)` builds final result

### C) Dedup wrapper request

1. wrapper creates store + policy and child context
2. child executes under dedup/refill control
3. executor performs acceptance/arbitration + bounded refills
4. store commits; wrapper writes merger cursor state

### D) View-session request

1. wrapper resolves cache key
2. cache miss/new session -> regenerate and cache
3. cache hit -> load session list from Redis
4. return requested slice + advanced merger page
