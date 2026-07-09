"""Dedup correctness with sources that genuinely contain duplicates.

Zero-duplicate sources can prove *item loss* but not *dedup correctness*. These
tests inject real duplicates (overlapping sources, cross-page repeats, within-
batch repeats) and assert dedup removes exactly the dupes while surfacing every
unique id.

These exercise dedup across overlapping sources, cross-page/cross-rebuild repeats,
priority arbitration, and every merger type, at both overfetch=1 and overfetch=4.
"""

import pytest
import fakeredis.aioredis

from tests import sources as S
from smartfeed.models.subfeed import SubFeed
from smartfeed.models.mixers import (
    MergerAppend,
    MergerAppendDistribute,
    MergerPercentage,
    MergerPercentageItem,
    MergerPositional,
)
from smartfeed.execution import executor as run_executor


def _redis():
    return fakeredis.aioredis.FakeRedis()


# ---------------------------------------------------------------------------
# Overlapping sources: every unique id exactly once
# ---------------------------------------------------------------------------


def _overlap_ctx():
    a = S.ScriptedSource(S.unique_pool(100, start=0))  # ids 0..99
    b = S.ScriptedSource(S.unique_pool(100, start=50))  # ids 50..149 (overlap 50..99)
    return a, b, S.make_ctx({"a": a, "b": b}, redis=_redis())


@pytest.mark.asyncio
async def test_overlapping_sources_cached_each_id_once():
    """session_size > universe -> one batch -> within-batch dedup handles overlap."""
    a, b, ctx = _overlap_ctx()
    merger = MergerAppend(
        node_id="m",
        items=[
            SubFeed(subfeed_id="a", method_name="a"),
            SubFeed(subfeed_id="b", method_name="b"),
        ],
    )
    node = S.wrapper(merger, session_size=500, dedup_key="id")
    pages = await S.drain(node, ctx, limit=10, max_pages=100)
    S.assert_no_duplicates(pages)
    S.assert_full_coverage(pages, set(range(150)))


@pytest.mark.asyncio
async def test_overlapping_sources_passthrough_each_id_once():
    """Passthrough cross-page dedup via the Redis seen-set (overfetch=1 -> no loss)."""
    a, b, ctx = _overlap_ctx()
    merger = MergerAppend(
        node_id="m",
        items=[
            SubFeed(subfeed_id="a", method_name="a"),
            SubFeed(subfeed_id="b", method_name="b"),
        ],
    )
    node = S.wrapper(merger, dedup_key="id")
    pages = await S.drain(node, ctx, limit=10, max_pages=200)
    S.assert_no_duplicates(pages)
    S.assert_full_coverage(pages, set(range(150)))


# ---------------------------------------------------------------------------
# Within-batch high-density duplicates collapse
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adjacent_duplicates_collapse_within_batch():
    src = S.ScriptedSource(S.adjacent_dup_pool(80))  # each id emitted twice, back-to-back
    node = S.wrapper(S.subfeed("src", "src"), session_size=500, dedup_key="id")
    ctx = S.make_ctx({"src": src}, redis=_redis())
    pages = await S.drain(node, ctx, limit=10, max_pages=100)
    S.assert_no_duplicates(pages)
    S.assert_full_coverage(pages, set(range(80)))


# ---------------------------------------------------------------------------
# Cross-page duplicate suppression (passthrough seen-set)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_page_duplicate_suppressed_passthrough():
    # id 3 appears at pos 3 and again at pos 25 (a later page).
    pool = S.unique_pool(30)
    pool.insert(25, {"id": 3, "val": "v3-again"})
    src = S.ScriptedSource(pool)
    node = S.wrapper(S.subfeed("src", "src"), dedup_key="id")
    ctx = S.make_ctx({"src": src}, redis=_redis())
    pages = await S.drain(node, ctx, limit=5, max_pages=50)
    ids = S.flat_ids(pages)
    assert ids.count(3) == 1, f"cross-page duplicate not suppressed: id 3 served {ids.count(3)}x"


# ---------------------------------------------------------------------------
# Cross-REBUILD dedup: within one scroll, a shown id must not reappear after a
# rebuild boundary (session-scoped seen-set carried across cold rebuilds).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cached_dedup_suppresses_across_rebuild_boundary():
    # id 3 at pos 3 (batch 1) and pos 20 (batch 2, after session_size=20 boundary).
    pool = S.unique_pool(20)
    pool.append({"id": 3, "val": "v3-again"})  # pos 20
    src = S.ScriptedSource(pool)
    node = S.wrapper(S.subfeed("src", "src"), session_size=20, dedup_key="id")
    ctx = S.make_ctx({"src": src}, redis=_redis())
    pages = await S.drain(node, ctx, limit=10, max_pages=50)
    assert S.flat_ids(pages).count(3) == 1


# ---------------------------------------------------------------------------
# Priority arbitration
# ---------------------------------------------------------------------------


def _prio_wrapper():
    merger = MergerAppend(
        node_id="m",
        items=[
            SubFeed(subfeed_id="lo", method_name="x", dedup_priority=1),
            SubFeed(subfeed_id="hi", method_name="x", dedup_priority=5),
        ],
    )
    return S.wrapper(merger, dedup_key="id")


def _stamped(id_, source):
    return {"id": id_, "_smartfeed_debug_info": {"source": source}}


def _winner_source(item):
    return (item.get("_smartfeed_debug_info") or {}).get("source")


# Regression guard: in-batch priority arbitration must run even when the
# higher-priority copy is NOT the first-seen one. The seen-check skips only true
# cross-page dupes (`key in seen and key not in batch`), so a within-batch duplicate
# still reaches priority arbitration.
@pytest.mark.asyncio
async def test_priority_higher_source_wins_when_listed_second():
    w = _prio_wrapper()
    # low-prio at index 0, high-prio dup at index 1: higher priority must win.
    out = w._dedup([_stamped(7, "lo"), _stamped(7, "hi")])
    assert len(out) == 1
    assert _winner_source(out[0]) == "hi", f"lower-priority first-seen copy won: {out}"


@pytest.mark.asyncio
async def test_priority_higher_source_wins_integration_merger_order():
    """Integration: MergerAppend lists the low-priority source first, so its copy is
    first-seen; the high-priority source (listed second) must still win the key."""
    lo = S.ScriptedSource([{"id": 0, "val": "lo"}])
    hi = S.ScriptedSource([{"id": 0, "val": "hi"}])
    merger = MergerAppend(
        node_id="m",
        items=[
            SubFeed(subfeed_id="lo", method_name="lo", dedup_priority=1),
            SubFeed(subfeed_id="hi", method_name="hi", dedup_priority=5),
        ],
    )
    node = S.wrapper(merger, session_size=100, dedup_key="id")
    ctx = S.make_ctx({"lo": lo, "hi": hi}, redis=_redis())
    r = await run_executor.run(node, ctx, limit=10, cursor={})
    kept = [it for it in r.data if it["id"] == 0]
    assert len(kept) == 1
    assert kept[0]["val"] == "hi", f"higher-priority source lost to first-seen: {kept}"


@pytest.mark.asyncio
async def test_priority_preserved_when_high_arrives_in_refill():
    """Simulates the refill recombination: _dedup(data + refill) where the high-
    priority copy only shows up in the refill batch. It must still win, and the
    stale copy must be removed (assert the list directly so a fix that keeps the
    dupe cannot pass)."""
    w = _prio_wrapper()
    data = [_stamped(1, "lo"), _stamped(7, "lo")]  # first fetch
    refill = [_stamped(7, "hi"), _stamped(2, "hi")]  # refill batch
    out = w._dedup(data + refill)
    assert [it["id"] for it in out] == [1, 7, 2], f"expected one copy each in order: {out}"
    assert _winner_source(out[1]) == "hi", f"high-priority refill copy lost: {out}"


@pytest.mark.asyncio
async def test_priority_defaults_zero_without_source_stamp():
    w = _prio_wrapper()
    out = w._dedup([{"id": 8}, _stamped(8, "hi")])  # unstamped (prio 0) then hi (prio 5)
    assert len(out) == 1
    assert _winner_source(out[0]) == "hi", f"unstamped prio-0 copy beat higher priority: {out}"


@pytest.mark.asyncio
async def test_priority_cross_page_shown_item_wins_documented():
    """Characterization: cross-page, an already-shown (lower-priority) item cannot be
    retracted when a higher-priority duplicate appears on a later page."""
    # page 1 shows id 5 from the low-priority source; page 2 has id 5 from high-prio.
    lo = S.ScriptedSource([{"id": 5, "val": "lo"}, {"id": 6, "val": "lo"}])
    hi = S.ScriptedSource([{"id": 5, "val": "hi"}, {"id": 7, "val": "hi"}])
    merger = MergerAppend(
        node_id="m",
        items=[
            SubFeed(subfeed_id="lo", method_name="lo", dedup_priority=1),
            SubFeed(subfeed_id="hi", method_name="hi", dedup_priority=5),
        ],
    )
    node = S.wrapper(merger, dedup_key="id")
    ctx = S.make_ctx({"lo": lo, "hi": hi}, redis=_redis())
    r1 = await run_executor.run(node, ctx, limit=1, cursor={})
    # Whatever is shown first for id 5 is final; it is never served twice.
    r2 = await run_executor.run(node, ctx, limit=3, cursor=r1.next_page)
    ids = [it["id"] for it in r1.data] + [it["id"] for it in r2.data]
    assert ids.count(5) == 1


# ---------------------------------------------------------------------------
# Dedup coverage holds through every merger type (regression guard).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dedup_over_distribute_covers_all():
    pool = [{"id": k, "grp": k % 5} for k in range(120)]
    src = S.ScriptedSource(pool)
    merger = MergerAppendDistribute(
        node_id="m", items=[SubFeed(subfeed_id="a", method_name="a")], distribution_key="grp"
    )
    node = S.wrapper(merger, session_size=20, dedup_key="id")
    ctx = S.make_ctx({"a": src}, redis=_redis())
    pages = await S.drain(node, ctx, limit=10, max_pages=300)
    S.assert_full_coverage(pages, set(range(120)))
    S.assert_no_duplicates(pages)


@pytest.mark.asyncio
async def test_dedup_over_percentage_covers_all():
    src = S.ScriptedSource(S.unique_pool(120))
    merger = MergerPercentage(
        node_id="m",
        items=[
            MergerPercentageItem(percentage=100, data=SubFeed(subfeed_id="a", method_name="a")),
        ],
    )
    node = S.wrapper(merger, session_size=20, dedup_key="id")
    ctx = S.make_ctx({"a": src}, redis=_redis())
    pages = await S.drain(node, ctx, limit=10, max_pages=300)
    S.assert_full_coverage(pages, set(range(120)))
    S.assert_no_duplicates(pages)


@pytest.mark.asyncio
async def test_dedup_over_positional_covers_all():
    a = S.ScriptedSource(S.unique_pool(120, start=0))  # ids 0..119
    b = S.ScriptedSource(S.unique_pool(60, start=1000))  # ids 1000..1059
    merger = MergerPositional(
        node_id="m",
        positions=[1, 3, 5],
        positional=SubFeed(subfeed_id="a", method_name="a"),
        default=SubFeed(subfeed_id="b", method_name="b"),
    )
    node = S.wrapper(merger, session_size=20, dedup_key="id")
    ctx = S.make_ctx({"a": a, "b": b}, redis=_redis())
    pages = await S.drain(node, ctx, limit=10, max_pages=300)
    S.assert_full_coverage(pages, set(range(120)) | set(range(1000, 1060)))
    S.assert_no_duplicates(pages)


# ---------------------------------------------------------------------------
# Regression guard: dense duplicates must not produce short non-final
# pages -- the refill loop scans through duplicate runs until the page is full.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dense_duplicates_still_fill_pages():
    # Each unique id 1..79 is followed by three copies of a hot id (0): only ~1 in 4
    # fetched items is new, so a page needs several child fetches to fill.
    pool = []
    for k in range(1, 80):
        pool.append({"id": k})
        pool += [{"id": 0}] * 3
    src = S.ScriptedSource(pool)
    node = S.wrapper(S.subfeed("src", "src"), dedup_key="id")
    ctx = S.make_ctx({"src": src}, redis=_redis())
    pages = await S.drain(node, ctx, limit=10, max_pages=300)
    S.assert_pages_full(pages, limit=10)
