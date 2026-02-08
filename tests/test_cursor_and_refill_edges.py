import base64
import zlib

import pytest

from smartfeed.execution.context import ExecutionContext, RefillExecutionSettings
from smartfeed.execution.executor import Executor
from smartfeed.feed_models import BaseFeedConfigModel, FeedResult, FeedResultNextPage
from smartfeed.policies.dedup import DeduplicationPolicy
from smartfeed.policies.dedup_utils import decode_seen_from_cursor
from smartfeed.policies.seen_store import CursorSeenStore


class _DuplicateOnlyNode(BaseFeedConfigModel):
    """A node that always returns the same duplicate item and never ends."""

    type: str = "test_node"  # satisfies pydantic

    def __init__(self, **data):
        super().__init__(**data)
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
        return FeedResult(
            data=[{"id": "dup"} for _ in range(int(limit) or 1)],
            next_page=FeedResultNextPage(data={}),
            has_next_page=True,
        )


def _policy_with_seen_dup(*, max_refill_loops: int) -> tuple[DeduplicationPolicy, RefillExecutionSettings]:
    store = CursorSeenStore.from_after(
        after={"v": 2, "seen": [["dup", 10]]},
        cursor_compress=False,
        cursor_max_keys=None,
    )
    policy = DeduplicationPolicy(
        dedup_key="id",
        missing_key_policy="keep",
        store=store,
        seen_request_set=set(),
    )
    settings = RefillExecutionSettings(overfetch_factor=1, max_refill_loops=max_refill_loops)
    return policy, settings


@pytest.mark.asyncio
async def test_dedup_refill_stops_at_max_loops_when_only_duplicates() -> None:
    node = _DuplicateOnlyNode()
    executor = Executor()

    dedup_policy, settings = _policy_with_seen_dup(max_refill_loops=2)
    ctx = ExecutionContext(methods_dict={}, user_id="u", executor=executor)
    ctx.dedup = dedup_policy
    ctx.refill_settings = settings

    res = await executor.run(node, ctx, limit=3, next_page=FeedResultNextPage(data={}))

    assert res.data == []
    # initial call + 2 refill loops
    assert getattr(node, "calls") == 3


def test_decode_seen_from_cursor_raises_on_corrupt_compressed_payload() -> None:
    # invalid base64
    with pytest.raises(Exception):
        decode_seen_from_cursor({"v": 2, "c": "zlib+base64", "z": "not-base64"})

    # base64 ok, zlib invalid
    bad_zlib = base64.urlsafe_b64encode(b"not-a-zlib-stream").decode("ascii")
    with pytest.raises(Exception):
        decode_seen_from_cursor({"v": 2, "c": "zlib+base64", "z": bad_zlib})

    # zlib ok, json invalid
    bad_json = base64.urlsafe_b64encode(zlib.compress(b"not json")).decode("ascii")
    with pytest.raises(Exception):
        decode_seen_from_cursor({"v": 2, "c": "zlib+base64", "z": bad_json})


def test_decode_seen_from_cursor_rejects_wrong_version_or_codec() -> None:
    assert decode_seen_from_cursor({"v": 1, "c": "zlib+base64", "z": ""}) == {}
    assert decode_seen_from_cursor({"v": 2, "c": "other", "z": ""}) == {}
