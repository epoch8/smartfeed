import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

import pytest

from smartfeed.schemas import FeedResultNextPage, MergerDeduplication
from tests.fixtures import dedup_helpers as dh
from tests.fixtures.redis import redis_client  # noqa: F401
from tests.utils import parse_model


def _now_us() -> int:
    return time.perf_counter_ns() // 1000


@dataclass
class ChromeTraceRecorder:
    """Writes Chrome Trace Events JSON for chrome://tracing.

    This is intentionally tiny and test-only: no production dependencies.
    """

    pid: int = 1
    events: List[Dict[str, Any]] = field(default_factory=list)

    def _emit(self, event: Dict[str, Any]) -> None:
        self.events.append(event)

    def begin(self, name: str, *, tid: int, ts_us: Optional[int] = None, args: Optional[Dict[str, Any]] = None) -> None:
        self._emit(
            {
                "name": name,
                "ph": "B",
                "ts": int(_now_us() if ts_us is None else ts_us),
                "pid": int(self.pid),
                "tid": int(tid),
                "args": args or {},
            }
        )

    def end(self, name: str, *, tid: int, ts_us: Optional[int] = None, args: Optional[Dict[str, Any]] = None) -> None:
        self._emit(
            {
                "name": name,
                "ph": "E",
                "ts": int(_now_us() if ts_us is None else ts_us),
                "pid": int(self.pid),
                "tid": int(tid),
                "args": args or {},
            }
        )

    def instant(
        self, name: str, *, tid: int, ts_us: Optional[int] = None, args: Optional[Dict[str, Any]] = None
    ) -> None:
        self._emit(
            {
                "name": name,
                "ph": "i",
                "s": "t",
                "ts": int(_now_us() if ts_us is None else ts_us),
                "pid": int(self.pid),
                "tid": int(tid),
                "args": args or {},
            }
        )

    def write(self, path: str) -> None:
        payload = {"traceEvents": self.events}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)


class LoopBlockMonitor:
    """Detects event-loop blocking by measuring scheduling lag.

    If the event loop is blocked by long sync work, a periodic sleeper will wake
    up late; we track the maximum observed lag.
    """

    def __init__(self, *, sample_interval_s: float = 0.01, block_threshold_s: float = 0.25) -> None:
        self.sample_interval_s = float(sample_interval_s)
        self.block_threshold_s = float(block_threshold_s)
        self.max_lag_s: float = 0.0
        self.block_events: List[float] = []
        self._task: Optional[asyncio.Task[None]] = None
        self._stop = asyncio.Event()

    async def __aenter__(self) -> "LoopBlockMonitor":
        self._stop.clear()
        self._task = asyncio.create_task(self._run())
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        self._stop.set()
        if self._task is not None:
            await self._task

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        expected = loop.time() + self.sample_interval_s
        while not self._stop.is_set():
            await asyncio.sleep(self.sample_interval_s)
            now = loop.time()
            lag = max(0.0, now - expected)
            expected = now + self.sample_interval_s
            self.max_lag_s = max(self.max_lag_s, lag)
            if lag >= self.block_threshold_s:
                self.block_events.append(lag)


@dataclass
class LeafConcurrencyTracker:
    """Tracks how many leaf calls are in-flight concurrently."""

    current: int = 0
    peak: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def enter(self) -> int:
        async with self._lock:
            self.current += 1
            if self.current > self.peak:
                self.peak = self.current
            return self.current

    async def exit(self) -> int:
        async with self._lock:
            self.current = max(0, self.current - 1)
            return self.current


def _trace_wrap_awaitable(
    rec: ChromeTraceRecorder, name: str, awaitable: Awaitable[Any], *, args: Dict[str, Any]
) -> Awaitable[Any]:
    async def _wrapped() -> Any:
        task = asyncio.current_task()
        tid = id(task) if task is not None else 0
        rec.begin(name, tid=tid, args=args)
        try:
            return await awaitable
        finally:
            rec.end(name, tid=tid)

    return _wrapped()


def _wrap_method_latency(method: Callable[..., Awaitable[Any]], *, latency_s: float) -> Callable[..., Awaitable[Any]]:
    async def _wrapped(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(latency_s)
        return await method(*args, **kwargs)

    return _wrapped


def _wrap_leaf_method_traced(
    *,
    rec: ChromeTraceRecorder,
    key: str,
    method: Callable[..., Awaitable[Any]],
    latency_s: float,
    concurrency: LeafConcurrencyTracker,
) -> Callable[..., Awaitable[Any]]:
    async def _wrapped(user_id: Any, limit: int, next_page: Any, **kwargs: Any) -> Any:
        task = asyncio.current_task()
        tid = id(task) if task is not None else 0

        page = getattr(next_page, "page", None)
        after = getattr(next_page, "after", None)
        after_type = type(after).__name__

        if after is None:
            after_preview = None
        else:
            after_preview = str(after)
            if len(after_preview) > 120:
                after_preview = after_preview[:117] + "..."

        span = f"leaf.{key}"

        in_flight = await concurrency.enter()
        rec.begin(
            span,
            tid=tid,
            args={
                "key": key,
                "limit": int(limit),
                "page": page,
                "after_type": after_type,
                "after_preview": after_preview,
                "in_flight": int(in_flight),
            },
        )
        try:
            if latency_s > 0:
                await asyncio.sleep(float(latency_s))
            return await method(user_id, limit, next_page, **kwargs)
        finally:
            rec.end(span, tid=tid)
            await concurrency.exit()

    return _wrapped


@pytest.mark.parametrize("redis_client", ["sync", "async"], indirect=True)
@pytest.mark.asyncio
async def test_async_loop_blocks_and_trace_for_deep_tree_all_mergers(redis_client, monkeypatch, tmp_path) -> None:
    """A smoke-test for detecting async loop blocks + visualizing concurrency.

    - Builds one deep tree that includes ALL merger types.
    - Simulates 2 sequential requests (fresh + next page).
    - Forces refills via positional under-fetch (`max_per_call=1`).
    - Records loop scheduling lag (blocks/hangs) and optionally exports a Chrome trace.

    Set `SMARTFEED_CHROME_TRACE=/path/to/trace.json` to write a trace.
    Open it in Chrome via chrome://tracing.
    """

    # Keep IDs disjoint across sources so "no dupes" is stable.
    # Refill waves are forced via max_per_call limits (under-fetch), not via dedup collisions.
    items_a = dh.make_items("A", 1, 400, user_id_mod=5, id_offset=1_000)
    items_b = dh.make_items("B", 1, 400, user_id_mod=5, id_offset=10_000)

    # Distribute branch: needs distribution_key present (user_id).
    items_posted_1 = dh.make_items("posted_1", 1, 80, user_id_mod=3, id_offset=20_000)
    items_posted_2 = dh.make_items("posted_2", 1, 120, user_id_mod=3, id_offset=21_000)

    # Gradient branch: overlapping ids again.
    items_g1 = dh.make_items("G1", 1, 250, user_id_mod=7, id_offset=30_000)
    items_g2 = dh.make_items("G2", 1, 250, user_id_mod=7, id_offset=40_000)

    # View-session leaf.
    items_vs = dh.make_items("VS", 1, 160, user_id_mod=11, id_offset=50_000)

    # Positional leaf that intentionally under-fetches to force refill waves.
    items_pos_leaf = dh.make_items("POS", 1, 500, user_id_mod=13, id_offset=60_000)

    # --- tracing (test-only monkeypatch) ---
    rec = ChromeTraceRecorder()
    leaf_concurrency = LeafConcurrencyTracker()
    pos_leaf_calls = {"count": 0}

    pos_leaf_base = dh.make_offset_paged_method(items_pos_leaf, max_per_call=1)

    async def _pos_leaf_counted(user_id: Any, limit: int, next_page: Any, **kwargs: Any) -> Any:
        pos_leaf_calls["count"] += 1
        return await pos_leaf_base(user_id, limit, next_page, **kwargs)

    # Leaf method tracing: wrap the *actual* subfeed method calls.
    # These spans are what you want to inspect for "are leaf calls parallel?".
    leaf_latency_s = 0.02
    methods_dict = {
        "a": _wrap_leaf_method_traced(
            rec=rec,
            key="a",
            method=dh.make_offset_paged_method(items_a),
            latency_s=leaf_latency_s,
            concurrency=leaf_concurrency,
        ),
        "b": _wrap_leaf_method_traced(
            rec=rec,
            key="b",
            method=dh.make_offset_paged_method(items_b),
            latency_s=leaf_latency_s,
            concurrency=leaf_concurrency,
        ),
        "posted_1": _wrap_leaf_method_traced(
            rec=rec,
            key="posted_1",
            method=dh.make_offset_paged_method(items_posted_1),
            latency_s=leaf_latency_s,
            concurrency=leaf_concurrency,
        ),
        "posted_2": _wrap_leaf_method_traced(
            rec=rec,
            key="posted_2",
            method=dh.make_offset_paged_method(items_posted_2),
            latency_s=leaf_latency_s,
            concurrency=leaf_concurrency,
        ),
        "g1": _wrap_leaf_method_traced(
            rec=rec,
            key="g1",
            method=dh.make_offset_paged_method(items_g1),
            latency_s=leaf_latency_s,
            concurrency=leaf_concurrency,
        ),
        "g2": _wrap_leaf_method_traced(
            rec=rec,
            key="g2",
            method=dh.make_offset_paged_method(items_g2),
            latency_s=leaf_latency_s,
            concurrency=leaf_concurrency,
        ),
        "vs": _wrap_leaf_method_traced(
            rec=rec,
            key="vs",
            method=dh.make_offset_paged_method(items_vs),
            latency_s=leaf_latency_s,
            concurrency=leaf_concurrency,
        ),
        # Fetch only 1 item per call even if demand is higher -> triggers refill loops.
        "pos_leaf": _wrap_leaf_method_traced(
            rec=rec,
            key="pos_leaf",
            method=_pos_leaf_counted,
            latency_s=leaf_latency_s,
            concurrency=leaf_concurrency,
        ),
    }

    view_session_cfg = {
        "merger_id": "vs_all",
        "type": "merger_view_session",
        "session_size": 100,
        "session_live_time": 60,
        "deduplicate": True,
        "dedup_key": "id",
        "data": dh._subfeed("sf_vs", "vs"),
    }

    pct_cfg = dh._percentage_config(
        "pct_all",
        items=dh._percentage_items(dh._subfeed("sf_a", "a"), dh._subfeed("sf_b", "b"), first_pct=50, second_pct=50),
    )

    pos_cfg = dh._positional_config(
        "pos_all",
        # Ensure positional inserts appear across pages for limit~12.
        # Use even positions so the schedule starts with the default branch;
        # this keeps ordering deterministic.
        positions=[2, 4, 6, 8, 10, 12, 14, 16, 18],
        positional=dh._subfeed("sf_pos_leaf", "pos_leaf"),
        default=pct_cfg,
    )

    dist_cfg = dh._distribute_config(
        "dist_all",
        items=[dh._subfeed("sf_posted_1", "posted_1"), dh._subfeed("sf_posted_2", "posted_2")],
        distribution_key="user_id",
    )

    grad_cfg = dh._gradient_config(
        "grad_all",
        item_from={"percentage": 70, "data": dh._subfeed("sf_g1", "g1")},
        item_to={"percentage": 30, "data": dh._subfeed("sf_g2", "g2")},
        step=10,
        size_to_step=5,
        shuffle=False,
    )

    # Include all merger types as siblings so they are executed (and visible in trace),
    # while keeping the main output driven by the first branch.
    deep_tree = dh._append_config("append_all", [pos_cfg, view_session_cfg, dist_cfg, grad_cfg])
    config = dh._dedup_config(
        "dedup_all",
        deep_tree,
        dedup_key="id",
        state_backend="cursor",
        overfetch_factor=3,
        max_refill_loops=50,
    )
    merger = parse_model(MergerDeduplication, config)

    # Patch Executor.gather to wrap each awaitable for Chrome trace.
    from smartfeed.execution.executor import Executor  # local import for monkeypatch

    original_gather = Executor.gather

    async def _gather_traced(self: Any, *coros: Any) -> List[Any]:
        wrapped = [
            _trace_wrap_awaitable(rec, "executor.gather.op", c, args={"idx": i, "total": len(coros)})
            for i, c in enumerate(coros)
        ]
        task = asyncio.current_task()
        tid = id(task) if task is not None else 0
        rec.begin("executor.gather", tid=tid, args={"n": len(coros)})
        try:
            return await original_gather(self, *wrapped)
        finally:
            rec.end("executor.gather", tid=tid)

    monkeypatch.setattr(Executor, "gather", _gather_traced)

    # Patch Executor.run to show sequential refill loops vs plan execution.
    original_run = Executor.run

    async def _run_traced(
        self: Any,
        node: Any,
        ctx: Any,
        limit: int,
        next_page: Any,
        **params: Any,
    ) -> Any:
        task = asyncio.current_task()
        tid = id(task) if task is not None else 0
        node_type = getattr(node, "type", node.__class__.__name__)
        node_id = getattr(node, "merger_id", getattr(node, "subfeed_id", None))
        rec.begin(
            "executor.run_node",
            tid=tid,
            args={"node_type": node_type, "node_id": node_id, "limit": int(limit)},
        )
        try:
            return await original_run(self, node, ctx, limit, next_page, **params)
        finally:
            rec.end("executor.run_node", tid=tid)

    monkeypatch.setattr(Executor, "run", _run_traced)

    # --- run: fresh request + next_page ---
    limit = 12
    np0 = FeedResultNextPage(data={})

    async with LoopBlockMonitor(sample_interval_s=0.01, block_threshold_s=0.05) as monitor:
        res1 = await asyncio.wait_for(
            merger.get_data(
                methods_dict=methods_dict,
                user_id="u",
                limit=limit,
                next_page=np0,
                redis_client=redis_client,
            ),
            timeout=15,
        )
        res2 = await asyncio.wait_for(
            merger.get_data(
                methods_dict=methods_dict,
                user_id="u",
                limit=limit,
                next_page=res1.next_page,
                redis_client=redis_client,
            ),
            timeout=15,
        )

    # Sanity: we should fill the page and maintain dedup invariants.
    assert len(res1.data) == limit
    assert len({x["id"] for x in res1.data}) == limit
    assert len(res2.data) == limit
    assert len({x["id"] for x in res2.data}) == limit

    # Hard assertion: leaf calls must overlap (async concurrency), not serialize.
    assert leaf_concurrency.peak > 1
    # Refill signal: with max_per_call=1, two page requests should trigger
    # multiple extra positional calls to satisfy positional slots.
    assert pos_leaf_calls["count"] > 2

    # Primary signal: event-loop should remain responsive under load.
    assert monitor.max_lag_s < 0.1

    out = os.environ.get("SMARTFEED_CHROME_TRACE")
    if out:
        # Allow writing to an explicit file path, or to a directory.
        out_path = out
        if os.path.isdir(out_path):
            out_path = os.path.join(out_path, "smartfeed_trace.json")
        rec.instant("loop.max_lag", tid=0, args={"max_lag_s": monitor.max_lag_s, "blocks": len(monitor.block_events)})
        rec.write(out_path)

    # Keep references so this test remains useful in local debugging.
    _ = tmp_path
