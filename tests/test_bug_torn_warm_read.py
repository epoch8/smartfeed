"""Regression: torn warm read -- meta and data are two sequential
GETs, so a rebuild landing between them pairs OLD-gen meta with the NEW batch.

`_execute_with_cache` reads `:meta` (gen matches the cursor -> proceed), then
reads the data key. `_write_cache` replaces both keys atomically in one
pipeline, so a rebuild that lands between the two GETs makes the warm reader
serve the REPLACEMENT batch at the OLD scroll's offset under the OLD gen:
items from a batch the client's gen never belonged to, while page 2 of the
original batch is silently skipped.

Correct behavior: the served page must be consistent with ONE snapshot --
either the old batch at the old offset, or a clean rebuild (new gen from
offset 0). This test forces the interleaving deterministically by triggering a
fresh-scroll rebuild from a hook between the two GETs.
"""

from typing import Awaitable, Callable, Optional

import pytest
import fakeredis.aioredis

from smartfeed.models.base import FeedResult
from tests import sources as S
from smartfeed.execution import executor as run_executor

GetHook = Callable[[str, Optional[bytes]], Awaitable[None]]


class HookedRedis:
    """Proxy that awaits a hook after each GET returns (to interleave a racing
    writer between the wrapper's meta GET and data GET)."""

    def __init__(self, inner):
        self._inner = inner
        self.on_get: Optional[GetHook] = None

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def get(self, key):
        val = await self._inner.get(key)
        if self.on_get is not None:
            await self.on_get(key, val)
        return val


class ShiftingSource:
    """Returns a disjoint id range on every call, so each rebuild produces a
    recognizably different batch."""

    def __init__(self):
        self.calls = 0

    async def __call__(self, user_id, limit, next_page, **kw) -> FeedResult:
        base = self.calls * 100
        self.calls += 1
        data = [{"id": base + i, "val": f"v{base + i}"} for i in range(limit)]
        return FeedResult(data=data, next_page={}, has_next_page=False)


@pytest.mark.asyncio
async def test_warm_read_is_snapshot_consistent_under_racing_rebuild():
    hooked = HookedRedis(fakeredis.aioredis.FakeRedis())
    node = S.wrapper(S.subfeed("src", "src"), session_size=10)
    ctx = S.make_ctx({"src": ShiftingSource()}, redis=hooked)

    r1 = await run_executor.run(node, ctx, limit=5, cursor={})  # batch [0..9], gen g1
    gen_1 = r1.next_page["w"]["gen"]
    assert [it["id"] for it in r1.data] == [0, 1, 2, 3, 4]

    async def rebuild_between_reads(key, val):
        if not key.endswith(":meta") or val is None:
            return
        hooked.on_get = None  # fire once
        # A fresh scroll (empty cursor) rebuilds the batch -> [100..109], new gen.
        await run_executor.run(node, ctx, limit=5, cursor={})

    hooked.on_get = rebuild_between_reads
    r2 = await run_executor.run(node, ctx, limit=5, cursor=r1.next_page)

    ids_2 = [it["id"] for it in r2.data]
    gen_2 = r2.next_page["w"]["gen"]
    assert ids_2 == [5, 6, 7, 8, 9] or gen_2 != gen_1, (
        f"torn read: served {ids_2} from the replacement batch under the old gen -- "
        f"meta and data must be read as one snapshot (single MGET/pipeline)"
    )
