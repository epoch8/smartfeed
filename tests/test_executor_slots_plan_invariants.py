import pytest

from smartfeed.execution.context import ExecutionContext
from smartfeed.execution.executor import Executor
from smartfeed.execution.plans import SlotSpec, SlotsPlan
from smartfeed.feed_models import BaseFeedConfigModel, FeedResult, FeedResultNextPage, FeedResultNextPageInside
from smartfeed.policies.dedup import DeduplicationPolicy
from smartfeed.policies.seen_store import CursorSeenStore


class _Owner(BaseFeedConfigModel):
    type: str = "test_owner"

    def __init__(self, *, name: str, **data):
        super().__init__(**data)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "last_limit", None)
        object.__setattr__(self, "calls", 0)

    async def get_data(  # type: ignore[override]
        self,
        methods_dict,
        user_id,
        limit,
        next_page,
        redis_client=None,
        ctx=None,
        **params,
    ) -> FeedResult:
        object.__setattr__(self, "calls", int(getattr(self, "calls", 0)) + 1)
        object.__setattr__(self, "last_limit", int(limit))
        return FeedResult(data=[self.name] * int(limit), next_page=FeedResultNextPage(data={}), has_next_page=False)


class _PagedOwner(BaseFeedConfigModel):
    type: str = "test_paged_owner"
    subfeed_id: str
    total: int = 10

    def __init__(self, *, subfeed_id: str, total: int = 10, **data):
        super().__init__(subfeed_id=subfeed_id, total=total, **data)
        object.__setattr__(self, "calls", 0)
        object.__setattr__(self, "limits", [])

    async def get_data(  # type: ignore[override]
        self,
        methods_dict,
        user_id,
        limit,
        next_page,
        redis_client=None,
        ctx=None,
        **params,
    ) -> FeedResult:
        object.__setattr__(self, "calls", int(getattr(self, "calls", 0)) + 1)
        limits = list(getattr(self, "limits", []))
        limits.append(int(limit))
        object.__setattr__(self, "limits", limits)

        entry = next_page.data.get(self.subfeed_id)
        offset = int(entry.after) if (entry is not None and isinstance(entry.after, int)) else 0

        take = max(0, min(int(limit), int(self.total) - offset))
        data = [{"id": f"{self.subfeed_id}_{i}"} for i in range(offset + 1, offset + take + 1)]
        new_after = offset + take

        next_page.data[self.subfeed_id] = FeedResultNextPageInside(
            page=(entry.page + 1 if entry is not None else 2),
            after=new_after,
        )
        return FeedResult(
            data=data,
            next_page=next_page,
            has_next_page=bool(new_after < int(self.total)),
        )


def _dedup_policy() -> DeduplicationPolicy:
    store = CursorSeenStore.from_after(after=None, cursor_compress=False, cursor_max_keys=None)
    return DeduplicationPolicy(
        dedup_key="id",
        missing_key_policy="keep",  # type: ignore[arg-type]
        store=store,
        seen_request_set=set(),
    )


@pytest.mark.asyncio
async def test_slots_plan_limit_le_zero_calls_assemble_only() -> None:
    executor = Executor()
    ctx = ExecutionContext(methods_dict={}, user_id="u", executor=executor)

    owner = _Owner(name="x")

    called = {"assemble": 0}

    def assemble(output, next_page, owner_results):
        called["assemble"] += 1
        assert output == []
        assert owner_results == {}
        return FeedResult(data=output, next_page=next_page, has_next_page=False)

    plan = SlotsPlan(
        ctx=ctx,
        limit=0,
        next_page=FeedResultNextPage(data={}),
        params={},
        slots=[SlotSpec(owner=owner, max_count=10)],
        assemble=assemble,
    )

    res = await executor.execute_plan(plan)
    assert res.data == []
    assert called["assemble"] == 1
    assert getattr(owner, "calls") == 0


@pytest.mark.asyncio
async def test_slots_plan_owner_fetch_limits_overrides_demand() -> None:
    executor = Executor()
    ctx = ExecutionContext(methods_dict={}, user_id="u", executor=executor)

    owner = _Owner(name="x")

    def assemble(output, next_page, owner_results):
        return FeedResult(data=output, next_page=next_page, has_next_page=False)

    plan = SlotsPlan(
        ctx=ctx,
        limit=5,
        next_page=FeedResultNextPage(data={}),
        params={},
        slots=[SlotSpec(owner=owner, max_count=5)],
        assemble=assemble,
        owner_fetch_limits={id(owner): 1},
    )

    res = await executor.execute_plan(plan)
    assert getattr(owner, "calls") == 1
    assert getattr(owner, "last_limit") == 1
    assert res.data == ["x"]


@pytest.mark.asyncio
async def test_slots_plan_no_ops_path_assemble_still_runs() -> None:
    executor = Executor()
    ctx = ExecutionContext(methods_dict={}, user_id="u", executor=executor)

    owner = _Owner(name="x")

    called = {"assemble": 0}

    def assemble(output, next_page, owner_results):
        called["assemble"] += 1
        assert output == []
        assert owner_results == {}
        return FeedResult(data=[], next_page=next_page, has_next_page=False)

    plan = SlotsPlan(
        ctx=ctx,
        limit=5,
        next_page=FeedResultNextPage(data={}),
        params={},
        slots=[SlotSpec(owner=owner, max_count=0)],
        assemble=assemble,
    )

    res = await executor.execute_plan(plan)
    assert res.data == []
    assert called["assemble"] == 1
    assert getattr(owner, "calls") == 0


@pytest.mark.asyncio
async def test_slots_plan_quota_deficit_triggers_refill_wave() -> None:
    executor = Executor()
    ctx = ExecutionContext(methods_dict={}, user_id="u", executor=executor)
    ctx.dedup = _dedup_policy()

    a = _PagedOwner(subfeed_id="a", total=10)
    b = _PagedOwner(subfeed_id="b", total=10)

    def assemble(output, next_page, owner_results):
        return FeedResult(data=output, next_page=next_page, has_next_page=False)

    plan = SlotsPlan(
        ctx=ctx,
        limit=6,
        next_page=FeedResultNextPage(data={
            "a": FeedResultNextPageInside(page=1, after=0),
            "b": FeedResultNextPageInside(page=1, after=0),
        }),
        params={},
        slots=[
            SlotSpec(owner=a, max_count=3),
            SlotSpec(owner=b, max_count=3),
        ],
        assemble=assemble,
        # Force an initial under-fetch for owner a (quota deficit).
        owner_fetch_limits={id(a): 1},
    )

    res = await executor.execute_plan(plan)

    # a should be refilled from 1 -> 3 items
    assert getattr(a, "calls") >= 2
    assert res.data[:3] == [{"id": "a_1"}, {"id": "a_2"}, {"id": "a_3"}]
    assert res.data[3:] == [{"id": "b_1"}, {"id": "b_2"}, {"id": "b_3"}]


@pytest.mark.asyncio
async def test_slots_plan_quota_deficit_stops_refill_when_owner_exhausts() -> None:
    executor = Executor()
    ctx = ExecutionContext(methods_dict={}, user_id="u", executor=executor)
    ctx.dedup = _dedup_policy()

    # Owner a can never satisfy its full slot quota.
    a = _PagedOwner(subfeed_id="a", total=2)
    b = _PagedOwner(subfeed_id="b", total=10)

    def assemble(output, next_page, owner_results):
        return FeedResult(data=output, next_page=next_page, has_next_page=False)

    plan = SlotsPlan(
        ctx=ctx,
        limit=6,
        next_page=FeedResultNextPage(
            data={
                "a": FeedResultNextPageInside(page=1, after=0),
                "b": FeedResultNextPageInside(page=1, after=0),
            }
        ),
        params={},
        slots=[
            SlotSpec(owner=a, max_count=3),
            SlotSpec(owner=b, max_count=3),
        ],
        assemble=assemble,
        # Force an initial under-fetch to create a quota deficit for a.
        owner_fetch_limits={id(a): 1},
    )

    res = await executor.execute_plan(plan)

    # a is exhausted after returning 2 total items; refill should stop.
    assert getattr(a, "calls") == 2
    assert getattr(a, "limits") == [1, 2]
    assert getattr(b, "calls") == 1

    assert res.data == [
        {"id": "a_1"},
        {"id": "a_2"},
        {"id": "b_1"},
        {"id": "b_2"},
        {"id": "b_3"},
    ]
