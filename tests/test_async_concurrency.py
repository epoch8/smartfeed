"""Test that subfeeds in parallel branches run concurrently via asyncio.gather,
and that the event loop stays responsive under load."""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional

import pytest

from smartfeed.models.base import FeedResult
from smartfeed.models.subfeed import SubFeed
from smartfeed.models.wrapper import Wrapper, WrapperCache, WrapperDedup
from smartfeed.models.mixers import MergerAppend, MergerPercentage, MergerPercentageItem, MergerPositional
from smartfeed.execution.context import ExecutionContext
from smartfeed.execution import executor as run_executor


# ---------------------------------------------------------------------------
# Helpers: concurrency tracker + loop block monitor
# ---------------------------------------------------------------------------

@dataclass
class ConcurrencyTracker:
    """Tracks how many leaf calls are in-flight at the same time."""
    current: int = 0
    peak: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def enter(self) -> None:
        async with self._lock:
            self.current += 1
            self.peak = max(self.peak, self.current)

    async def exit(self) -> None:
        async with self._lock:
            self.current -= 1


class LoopBlockMonitor:
    """Detects event-loop blocking by measuring scheduling lag."""

    def __init__(self, interval: float = 0.01, threshold: float = 0.1):
        self.interval = interval
        self.threshold = threshold
        self.max_lag: float = 0.0
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    async def __aenter__(self):
        self._stop.clear()
        self._task = asyncio.create_task(self._run())
        return self

    async def __aexit__(self, *exc):
        self._stop.set()
        if self._task:
            await self._task

    async def _run(self):
        loop = asyncio.get_running_loop()
        expected = loop.time() + self.interval
        while not self._stop.is_set():
            await asyncio.sleep(self.interval)
            now = loop.time()
            lag = max(0.0, now - expected)
            self.max_lag = max(self.max_lag, lag)
            expected = now + self.interval


# ---------------------------------------------------------------------------
# Slow subfeed factory
# ---------------------------------------------------------------------------

def make_slow_subfeed_method(
    source: str,
    latency: float,
    tracker: ConcurrencyTracker,
    call_counter: dict,
):
    """Create an async subfeed method that sleeps (simulating I/O) and tracks concurrency."""

    async def method(user_id: str, limit: int, next_page: dict, **kw) -> FeedResult:
        call_counter[source] = call_counter.get(source, 0) + 1
        page = next_page.get("page", 1)
        start = (page - 1) * limit

        await tracker.enter()
        try:
            await asyncio.sleep(latency)
        finally:
            await tracker.exit()

        # Each source gets unique id ranges to avoid dedup collisions
        offset = hash(source) % 100_000
        data = [{"id": offset + start + i, "val": source} for i in range(limit)]
        return FeedResult(
            data=data,
            next_page={"page": page + 1},
            has_next_page=True,
        )

    return method


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestParallelSubfeeds:
    """Verify that sibling subfeeds in a mixer run concurrently."""

    @pytest.mark.asyncio
    async def test_two_subfeeds_run_in_parallel(self):
        tracker = ConcurrencyTracker()
        calls = {}
        latency = 0.05

        methods = {
            "slow_a": make_slow_subfeed_method("a", latency, tracker, calls),
            "slow_b": make_slow_subfeed_method("b", latency, tracker, calls),
        }
        ctx = ExecutionContext(session_id="s1", methods_dict=methods, redis=None)
        executor = None

        node = MergerAppend(
            node_id="par",
            items=[
                SubFeed(subfeed_id="a", method_name="slow_a"),
                SubFeed(subfeed_id="b", method_name="slow_b"),
            ],
        )

        t0 = time.monotonic()
        result = await run_executor.run(node, ctx, limit=10, cursor={})
        elapsed = time.monotonic() - t0

        assert len(result.data) == 10
        # Both called
        assert calls.get("a", 0) >= 1
        assert calls.get("b", 0) >= 1
        # Peak concurrency > 1 means they ran in parallel
        assert tracker.peak > 1, f"Expected parallel execution, peak={tracker.peak}"
        # Total time should be ~latency (parallel), not ~2*latency (serial)
        assert elapsed < latency * 1.8, f"Took {elapsed:.3f}s, expected < {latency * 1.8:.3f}s"

    @pytest.mark.asyncio
    async def test_three_percentage_subfeeds_parallel(self):
        tracker = ConcurrencyTracker()
        calls = {}
        latency = 0.05

        methods = {
            "slow_a": make_slow_subfeed_method("a", latency, tracker, calls),
            "slow_b": make_slow_subfeed_method("b", latency, tracker, calls),
            "slow_c": make_slow_subfeed_method("c", latency, tracker, calls),
        }
        ctx = ExecutionContext(session_id="s1", methods_dict=methods, redis=None)
        executor = None

        node = MergerPercentage(
            node_id="pct",
            items=[
                MergerPercentageItem(percentage=40, data=SubFeed(subfeed_id="a", method_name="slow_a")),
                MergerPercentageItem(percentage=30, data=SubFeed(subfeed_id="b", method_name="slow_b")),
                MergerPercentageItem(percentage=30, data=SubFeed(subfeed_id="c", method_name="slow_c")),
            ],
        )

        t0 = time.monotonic()
        result = await run_executor.run(node, ctx, limit=10, cursor={})
        elapsed = time.monotonic() - t0

        assert len(result.data) == 10
        assert tracker.peak >= 2, f"Expected parallel, peak={tracker.peak}"
        assert elapsed < latency * 2.0

    @pytest.mark.asyncio
    async def test_positional_branches_parallel(self):
        """Positional's two branches (positional + default) should run in parallel."""
        tracker = ConcurrencyTracker()
        calls = {}
        latency = 0.05

        methods = {
            "slow_promo": make_slow_subfeed_method("promo", latency, tracker, calls),
            "slow_regular": make_slow_subfeed_method("regular", latency, tracker, calls),
        }
        ctx = ExecutionContext(session_id="s1", methods_dict=methods, redis=None)
        executor = None

        node = MergerPositional(
            node_id="pos",
            positions=[1, 3, 5],
            positional=SubFeed(subfeed_id="promo", method_name="slow_promo"),
            default=SubFeed(subfeed_id="regular", method_name="slow_regular"),
        )

        t0 = time.monotonic()
        result = await run_executor.run(node, ctx, limit=10, cursor={})
        elapsed = time.monotonic() - t0

        assert len(result.data) == 10
        assert tracker.peak > 1, f"Expected parallel, peak={tracker.peak}"
        assert elapsed < latency * 1.8


class TestEventLoopResponsiveness:
    """Verify the event loop stays unblocked during feed execution."""

    @pytest.mark.asyncio
    async def test_loop_not_blocked_under_load(self):
        tracker = ConcurrencyTracker()
        calls = {}
        latency = 0.02

        methods = {}
        subfeeds = []
        for i in range(5):
            name = f"source_{i}"
            methods[name] = make_slow_subfeed_method(name, latency, tracker, calls)
            subfeeds.append(SubFeed(subfeed_id=name, method_name=name))

        ctx = ExecutionContext(session_id="s1", methods_dict=methods, redis=None)
        executor = None

        node = MergerAppend(node_id="big", items=subfeeds)

        async with LoopBlockMonitor(interval=0.01, threshold=0.05) as monitor:
            result = await run_executor.run(node, ctx, limit=20, cursor={})

        assert len(result.data) == 20
        assert monitor.max_lag < 0.1, f"Event loop blocked: max_lag={monitor.max_lag:.3f}s"


class TestNestedParallelism:
    """Verify that nested mixer trees still execute leaves in parallel."""

    @pytest.mark.asyncio
    async def test_nested_mixers_parallel(self):
        """append(percentage(a, b), positional(c, d)) -- all 4 subfeeds should overlap."""
        tracker = ConcurrencyTracker()
        calls = {}
        latency = 0.05

        methods = {
            f"slow_{x}": make_slow_subfeed_method(x, latency, tracker, calls)
            for x in ("a", "b", "c", "d")
        }
        ctx = ExecutionContext(session_id="s1", methods_dict=methods, redis=None)
        executor = None

        node = MergerAppend(
            node_id="outer",
            items=[
                MergerPercentage(
                    node_id="pct",
                    items=[
                        MergerPercentageItem(percentage=50, data=SubFeed(subfeed_id="a", method_name="slow_a")),
                        MergerPercentageItem(percentage=50, data=SubFeed(subfeed_id="b", method_name="slow_b")),
                    ],
                ),
                MergerPositional(
                    node_id="pos",
                    positions=[1, 3],
                    positional=SubFeed(subfeed_id="c", method_name="slow_c"),
                    default=SubFeed(subfeed_id="d", method_name="slow_d"),
                ),
            ],
        )

        t0 = time.monotonic()
        result = await run_executor.run(node, ctx, limit=10, cursor={})
        elapsed = time.monotonic() - t0

        assert len(result.data) == 10
        # All 4 sources should have been called
        for x in ("a", "b", "c", "d"):
            assert calls.get(x, 0) >= 1
        # At least 2 in-flight at the same time
        assert tracker.peak >= 2, f"Expected parallel, peak={tracker.peak}"
        # Should take ~1x latency, not ~4x
        assert elapsed < latency * 2.5, f"Took {elapsed:.3f}s, suggests serial execution"
